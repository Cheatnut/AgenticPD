1-运行与配置系统

FrameworkConfig
config_snapshot.json

--parse-only 一个ORFS已有VARIANT的QoR查看器,会在runs/下新建session
功能类似trial_inspector.py

affects,作为判断checkpoint兼容性的依据

三层输出
session层:候选trial之索引trials.jsonl,优化树tree.json,配置快照config_snapshot.json,运行日志agenticpd.log
ORFS层:variant四大产物,reports/,results/,objects/,logs/
trial层:候选trial.json


SHA-256
每次创建Trial时候读取
environment_manifest.json
来计算SHA-256
写入TrialRecord.env_hash

2-智能搜索与决策系统

checkpoint记录可复用文件
优化树

llm到真实运行需要经过四重考验
1.给出的JSON可被解析
2.参数属于当前阶段且类型、范围通过校验
3.checkpoint通过完整性和兼容性检查
4.ORFS结束后有完整的finish QoR

由utils.qor_is_better()完成候选优胜判定

3-Trial与Checkpoint系统

数据模型schemas/trial.py

持久化managers/


StageResult

TrialRecord

CheckpointRef

FailureClass

trial_id随机八位十六进制字符串

TrialManager
CheckpointManager完整性和兼容性

param继承后的完整参数
param_diff相对于父trial的变化


4-ORFS执行与QoR系统

ORFS adapter


5-实验验证与回归系统


configs/experiments/
YAML独立实验声明,正式实验需要先冻结YAML
产生:人工根据实验设计编写的版本受控文件
先产生YAML实验声明，再运行实验，程序产生ORFS产物与runs/下session产物
消费:设计者消费,检验实际结果有无满足承诺;仅仅作为实验契约,main.py不调用，YAML不能强制约束
维护:发起并负责该实验的人，维护时间是实验开始之前，不是得到结果之后;实验跑完之后不要覆盖修改原有YANL，应该复制成一个新文件并升级名称

environment_manifest.json环境版本清单
config_snapshot.json某次session启动时运行配置副本:

main::build_config()生产
Optimizer._begin_trial()消费,每次创建Trial时Optimizer读取snapshot文件原始字节并计算SHA-256前16位
生成
TrialRecord.config_hash副本摘要
trial.json只保存hash












