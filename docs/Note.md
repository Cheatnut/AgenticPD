重构docs计划

1.如果用户没有明确要求，不得修改docs/,写入AGENTS.md和CLAUD.md
2.按照以下结构重构docs/下的文件(可复用已有文件)
3.文件名统一采用英文首字母大写+连字符+md的格式
4.先摆出框架，文件内部内容先不改动/写入

docs/
    - plans/
        - stage-c/
            - stage-c-plan.md
            - stage-c-check.md(阶段验收报告)
        - stage-d/
        - stage-e/
        - stage-f/
        - stage-g/
        - stage-h/
    - usage/
        - cli-verification.md
    - introduction/
        - 系统架构.md
        - <某细分系统>.md
    - Note.md(用户自己写，禁止改动)
    - AgenticPD八阶段迭代计划.md(总纲)
    - WORKFLOW.md
    - 阶段验收门模板.md
    - HANDOVER.md(当前阶段,待修复的问题等;每日工作结束后覆盖写入,第二天加载)
