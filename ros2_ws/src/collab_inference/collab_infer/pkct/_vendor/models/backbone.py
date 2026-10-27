"""
Backbone: 单目标跟踪主干网络 (MixFormer-Lite 风格)

One-stream tracker: 模板 (template) 和搜索区域 (search) 拼接为 token 序列,
通过 Transformer self-attention 做联合特征提取 + 模板-搜索交互。
比 Siamese 双分支精度更高、参数量相当。

输出三样东西:
1. fused_feature: 模板和搜索区域的 cross-attention 融合特征 [B, C, Hs, Ws]
2. search_feature: 搜索区域的特征图 (用于跟踪头 + 远程协同请求)
3. template_feature: 模板特征向量 (初始化时计算并缓存)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
from typing import Optional


class ConvStem(nn.Module):
    """
    轻量卷积 stem: 把图像 patch 化为 token 序列。
    用 EfficientNet-B0 的浅层做特征提取，再 1x1 conv 降维。
    """

    def __init__(self, out_dim: int = 192, stride: int = 16):
        super().__init__()
        # 取 EfficientNet-B0 的前几层 (stride=16)
        eff = models.efficientnet_b0(weights=None)
        # features[0:5] 大约把分辨率降到 1/16, 输出 80 channels
        self.stem = nn.Sequential(*list(eff.features.children())[:5])
        self.proj = nn.Conv2d(80, out_dim, kernel_size=1)
        self.norm = nn.LayerNorm(out_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: [B, 3, H, W]
        Returns:
            tokens: [B, H/16 * W/16, out_dim]
        """
        feat = self.stem(x)              # [B, 80, h, w]
        feat = self.proj(feat)            # [B, C, h, w]
        B, C, h, w = feat.shape
        tokens = feat.flatten(2).transpose(1, 2)  # [B, h*w, C]
        tokens = self.norm(tokens)
        return tokens, (h, w)


class MixedAttentionBlock(nn.Module):
    """
    MixFormer 风格的混合注意力块。

    在 PKCT 中简化为标准 MHSA + FFN; 由于模板和搜索 token 已经 concat,
    self-attention 自动包含了 template-self / search-self / template-search
    三种交互——这正是 one-stream tracker 的精髓。
    """

    def __init__(self, dim: int, num_heads: int = 6, mlp_ratio: float = 4.0,
                 dropout: float = 0.0):
        super().__init__()
        self.norm1 = nn.LayerNorm(dim)
        self.attn = nn.MultiheadAttention(dim, num_heads, dropout=dropout, batch_first=True)
        self.norm2 = nn.LayerNorm(dim)
        self.mlp = nn.Sequential(
            nn.Linear(dim, int(dim * mlp_ratio)),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(int(dim * mlp_ratio), dim),
            nn.Dropout(dropout),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        a = self.norm1(x)
        attn_out, _ = self.attn(a, a, a, need_weights=False)
        x = x + attn_out
        x = x + self.mlp(self.norm2(x))
        return x


class MixFormerLite(nn.Module):
    """
    MixFormer-Lite-style 跟踪主干。

    输入:
        template_img: [B, 3, Ht, Wt]  目标模板 (默认 128x128)
        search_img:   [B, 3, Hs, Ws]  搜索区域 (默认 256x256)

    输出 dict:
        search_feature:   [B, C, Hs/16, Ws/16]  搜索区域融合特征图 (跟踪头 + 远程协同)
        template_feature: [B, C]                模板的 pooled 特征向量
        feature_vec:      [B, feature_dim]      全局表征 (路由器输入)

    设计要点:
    - PKCI 的 Backbone 输出 (feature, local_logits)，跟踪 backbone 不输出
      logits, 而是输出特征图 (供下游 tracking_head 解码 bbox)
    - 与 PKCI 一样维持轻量化, 单帧推理 5-7ms (Orin FP16) 是目标
    """

    def __init__(
        self,
        embed_dim: int = 192,
        num_heads: int = 6,
        depth: int = 4,
        feature_dim: int = 256,
        template_size: int = 128,
        search_size: int = 256,
        stride: int = 16,
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.feature_dim = feature_dim
        self.template_size = template_size
        self.search_size = search_size
        self.stride = stride

        self.template_tokens_n = (template_size // stride) ** 2
        self.search_tokens_n = (search_size // stride) ** 2

        # 共享 stem: template/search 用同一个权重 (ImageNet 预训练)
        self.stem = ConvStem(out_dim=embed_dim, stride=stride)

        # 类型 embedding: 区分 template token 和 search token
        self.type_embed = nn.Embedding(2, embed_dim)  # 0=template, 1=search

        # 位置 embedding: 各自独立 (template / search 空间结构不同)
        self.pos_embed_t = nn.Parameter(torch.zeros(1, self.template_tokens_n, embed_dim))
        self.pos_embed_s = nn.Parameter(torch.zeros(1, self.search_tokens_n, embed_dim))
        nn.init.trunc_normal_(self.pos_embed_t, std=0.02)
        nn.init.trunc_normal_(self.pos_embed_s, std=0.02)

        # 混合注意力 blocks
        self.blocks = nn.ModuleList([
            MixedAttentionBlock(embed_dim, num_heads) for _ in range(depth)
        ])
        self.norm = nn.LayerNorm(embed_dim)

        # 全局特征向量 (路由器输入用)
        self.feature_proj = nn.Sequential(
            nn.Linear(embed_dim * 2, feature_dim),  # template+search 池化拼接
            nn.ReLU(inplace=True),
            nn.Dropout(0.1),
        )

    def forward(
        self,
        template_img: torch.Tensor,
        search_img: torch.Tensor,
        cached_template_tokens: Optional[tuple] = None,
    ) -> dict:
        B = search_img.shape[0]

        # Tokenize 模板与搜索. 推理时模板整段视频不变, 可传入 cached_template_tokens
        # (encode_template_tokens 的输出) 跳过模板 stem, 省掉每帧一半的 stem 计算.
        if cached_template_tokens is None:
            t_tokens, (ht, wt) = self.stem(template_img)   # [B, Nt, C]
        else:
            t_tokens, (ht, wt) = cached_template_tokens
        s_tokens, (hs, ws) = self.stem(search_img)     # [B, Ns, C]

        # 加位置 + 类型 embedding
        t_tokens = t_tokens + self.pos_embed_t[:, :t_tokens.size(1)]
        s_tokens = s_tokens + self.pos_embed_s[:, :s_tokens.size(1)]

        type_t = self.type_embed.weight[0].view(1, 1, -1)
        type_s = self.type_embed.weight[1].view(1, 1, -1)
        t_tokens = t_tokens + type_t
        s_tokens = s_tokens + type_s

        # 拼接为单一序列, 让 self-attention 做联合特征提取
        x = torch.cat([t_tokens, s_tokens], dim=1)     # [B, Nt+Ns, C]

        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)

        # 拆回 template / search
        Nt = t_tokens.size(1)
        t_out = x[:, :Nt]                               # [B, Nt, C]
        s_out = x[:, Nt:]                               # [B, Ns, C]

        # 搜索特征图重塑回 2D (供跟踪头使用)
        search_map = s_out.transpose(1, 2).reshape(B, self.embed_dim, hs, ws)

        # 模板池化为向量
        template_vec = t_out.mean(dim=1)                # [B, C]
        search_vec = s_out.mean(dim=1)                  # [B, C]

        # 全局特征向量
        feature_vec = self.feature_proj(
            torch.cat([template_vec, search_vec], dim=-1)
        )                                                # [B, feature_dim]

        return {
            "search_feature": search_map,
            "template_feature": template_vec,
            "feature_vec": feature_vec,
        }

    @torch.no_grad()
    def encode_template_tokens(self, template_img: torch.Tensor) -> tuple:
        """
        推理时调用: 仅跑模板 stem, 返回 (t_tokens, (ht, wt)) 供后续帧 reuse.
        把返回值作为 forward(..., cached_template_tokens=...) 传入即可跳过模板 stem.
        EfficientNet stem 是 backbone 里最贵的部分之一, 缓存它能显著降低单帧延迟.
        """
        return self.stem(template_img)

    @torch.no_grad()
    def encode_template(self, template_img: torch.Tensor) -> torch.Tensor:
        """
        初始化时调用: 仅编码模板 (含 pos embed), 简化版.
        如需跨帧缓存请用 encode_template_tokens + forward(cached_template_tokens=...).
        """
        t_tokens, _ = self.stem(template_img)
        t_tokens = t_tokens + self.pos_embed_t[:, :t_tokens.size(1)]
        return t_tokens
