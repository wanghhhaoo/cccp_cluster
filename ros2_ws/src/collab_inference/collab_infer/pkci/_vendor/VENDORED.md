从 pkci（commit 9d5b932）原样拷贝的推理所需文件：models/backbone.py、models/expert.py。
唯一改动：EfficientNet / MobileNet 构造改为 `weights=None`（不再联网下载 ImageNet 预训练权重；
checkpoint 覆盖全部参数，已核对 strict 加载无缺失键）。
