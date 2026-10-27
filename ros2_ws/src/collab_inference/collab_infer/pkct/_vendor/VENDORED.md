从 pkct（commit 5a7f8aa + 工作区未提交改动，默认行为不变）原样拷贝的推理所需文件：
models/{backbone,tracking_head,expert,router,temporal_memory,kalman_fusion,reid_fusion,global_state,pkct_system}.py、
communication/tracking_fragment.py、tracking/collab_system.py（放在 _vendor/collab_system.py）。
改动两处：models/backbone.py 的 EfficientNet-B0 构造改为 `weights=None`（checkpoint 覆盖全部参数，已核对无缺失键）；
models/expert.py 的专家初始化在 torch < 2.2（无 `generator` 参数，如 Jetson 的 torch 2.1）时退回普通初始化，初始值随后被 checkpoint 覆盖，不影响推理。
pkct_system.py 中 backbone_type="deit_tiny" 分支依赖未打包的 backbone_deit.py 与 timm，本包不使用。
