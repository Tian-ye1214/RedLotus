# 项目 Python 环境长路径失败记录

本记录只说明本地测试中项目 Python 环境的 provision 失败，不改变本轮 SQLite 性能门槛的结论。SQLite 的性能 gate 已阻止正式迁移；本项也仍为**未修复、未通过**。

## 复现范围

- 用例：`tests/test_execution_environment.py::test_python_command_is_provisioned_in_the_configured_project_environment`。
- 运行器：E 盘项目官方虚拟环境；依赖目录位于 D 盘。测试不发起 API 或网络请求。
- 新的诊断 basetemp 使用脱敏结构 `<E:\workspace\...\WorkDatabase\runtime\final-local-acceptance-v2\tmp-execution-environment-diagnostic-…>`，长度为 150 个字符；这是原验收 basetemp（末段为 `tmp`，108 个字符）的更长 E 盘同类位置。
- 单用例在 10.38 秒结束，退出为失败。既有验收产物没有被删除或复用。

## 可复现的失败位置

项目环境根目录随 pytest 临时用例路径展开。诊断运行中：

| 项目 | 脱敏路径结构 | 长度 |
| --- | --- | ---: |
| pytest 用例目录 | `<basetemp>\test_python_command_is_provisi0` | 182 |
| 环境根目录 | `<case>\runtime\environments\<24-char-project-id>` | 228 |
| 环境解释器 | `<environment>\Scripts\python.exe` | 247 |
| 失败目标 | `<case>\runtime\cache\<24-char-project-id>\tmp\<8-char-temp>\pip-25.0.1-py3-none-any.whl` | 265 |

第一阶段 `base-python -m venv --without-pip <environment>` 已成功：解释器文件存在，状态标记为 `python_ready`。第二阶段 `<environment>\Scripts\python.exe -m ensurepip --upgrade --default-pip` 以退出码 `1` 结束。`ensurepip` 在创建上表所示的 wheel 文件时得到 `FileNotFoundError: [Errno 2]`。

该临时目录来自项目执行环境的缓存变量：其有效结构为 `<cache>\<project-id>\tmp`。因此基础解释器可以启动，随后才在更深的临时 wheel 路径失败。265 字符的目标超过常见 Windows 260 字符路径界限；本次证据证明该长路径条件下的 provision 不能完成。

## 与原短路径产物的关系

原 `...\final-local-acceptance-v2\tmp` 是完整本地验收的正常隔离目录，用于保存每个用例的运行材料和失败证据。它仍应保留其原用途，不能通过把新的诊断改到更短目录来声称此功能通过。

原完整验收中该用例已经记录为失败，留下的环境标记同样停在 `python_ready`；但完整批次在 600 秒截止，未保留该单用例的完整 stderr。本文的更长路径单用例复现保留了完整的失败阶段、退出码和路径长度，因此用于定位动态临时路径限制，而不替代原验收产物。

## 当前状态

未修改应用源码、测试夹具或执行参数来规避此失败。后续若单独处理，应在项目运行目录内为 `ensurepip` bootstrap 提供更短的临时目录，并以当前长路径条件重新验证；该工作不在本轮 SQLite 性能 gate 的范围内。
