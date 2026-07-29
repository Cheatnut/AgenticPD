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







