# OpenViking / VikingBot 接入 SkillLearnBench

这个目录提供一个 OpenViking 原生的 SkillLearnBench runner，用来把
[SkillLearnBench](https://github.com/cxcscmu/SkillLearnBench) 的题目环境、
Docker verifier 和 OpenViking/VikingBot 的 agent 链路接起来。

runner 会复用每道 SkillLearnBench 题目自带的 Docker 环境和 `/tests/test.sh`
验收脚本，但不再使用题目原先面向 `claude`、`codex`、`gemini` 的 agent CLI。
实际解题由宿主机上的 VikingBot `AgentLoop` 完成；VikingBot 看到的工具会被替换成
Docker-backed 工具，通过 `docker exec` 把 shell 命令和文件读写转发到题目的
Docker 容器里执行。

## 本地跑单个题目

假设：

- 当前目录是 OpenViking 仓库根目录。
- Docker Desktop / Docker daemon 已经启动。
- OpenViking 配置文件已经存在：`~/.openviking/ov.conf`。
- `ov.conf` 里已经配置好 `bot.agents.model`、`bot.agents.api_key`、
  `bot.agents.api_base`、`bot.agents.provider`。

先准备 SkillLearnBench 仓库：

```bash
git clone https://github.com/cxcscmu/SkillLearnBench /private/tmp/SkillLearnBench
```

先做一次 preflight，确认 Docker、VikingBot runtime 和 `ov.conf` 都能正常加载。
推荐用当前项目的 uv 环境运行：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1 \
  --preflight
```

如果 preflight 里的 `ok` 是 `true`，就可以正式跑这个题目：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1
```

指定一个固定 trial id 会更方便定位结果：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1 \
  --trial-id local-smoke-001
```

这次运行的结果会写到：

```text
benchmark/skillLearnBench/result/no_skill/anthropic-poster-design/anthropic-poster-design-1/local-smoke-001/
```

### 单题调试：写入 OpenViking 记忆

当前默认评测就是 `no_skill`：不传 `--skill-source` 时，结果目录和 suite 汇总都会使用
`skill_config=no_skill`。

如果想把一次 no_skill 执行轨迹写入 OpenViking，让 OpenViking 从 agent 执行过程里生成
记忆，可以加 `--commit-trajectory-memory`：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1 \
  --trial-id memory-debug-001 \
  --hidden-verifier \
  --commit-trajectory-memory \
  --wait-memory-task
```

这个模式只要 VikingBot 产生了 `vikingbot-trajectory.json`，就会提交到 OpenViking；
不再依赖 verifier pass/fail。它会读取：

```text
benchmark/skillLearnBench/result/no_skill/<task>/<instance>/<trial-id>/agent/vikingbot-trajectory.json
```

然后构建一个 OpenViking session：

- `messages` 里的 `system/user/assistant/tool` 序列会被保留到 session 内容里。
- `assistant` 的可见输出会作为 assistant text part。
- `assistant.tool_calls` 和后续 `tool` result 会合并成 OpenViking `ToolPart`。
- `tools_used` 的 `tool_name/args/result/duration/execute_success` 会映射到
  `ToolPart.tool_name/tool_input/tool_output/duration_ms/tool_status`。
- `final_content` 会作为最后的 assistant 消息写入。

导入时要求 `ov.conf` 里使用 User API key，即 `bot.ov_server.api_key_type=user`。
每个 `<task>/<instance>` 会映射为一个稳定的 OpenViking peer，例如：

```text
anthropic-poster-design/anthropic-poster-design-1
-> slb-anthropic-poster-design-anthropic-poster-design-1
```

创建 session 时使用的 memory policy 是：

```json
{
  "self": {"enabled": true},
  "peer": {"enabled": true}
}
```

也就是说，导入时会同时开启 self 和 peer：普通 long-term memory 仍可按
`<task>/<instance>` 对应的 peer 隔离写入，便于后续按 instance 回放、检索和对比；
`trajectories` / `experiences` 这类 execution memory 也会进入 self 侧抽取链路，
用于沉淀 agent 执行轨迹和可复用经验。

如果已经跑过 no_skill，并且结果目录里已经有 `agent/vikingbot-trajectory.json`，
可以只导入已有轨迹，不重新构建 Docker、不重新跑 bot/verifier：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1 \
  --trial-id memory-debug-001 \
  --commit-trajectory-memory \
  --commit-existing-trajectory-memory \
  --wait-memory-task
```

### 单题调试：注入 OpenViking Resolution Pack

如果要模拟“OpenViking 已经从历史轨迹里沉淀出可复用经验/skill”，可以先把 no_skill
轨迹提交成 OpenViking memory，然后再开启 `--inject-search-resolution` 跑同一个 instance。
runner 会在 VikingBot 做题前调用 OpenViking `/api/v1/search/resolution`，把返回的
Query Resolution Pack 注入到 bot 的 system context 中。

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1 \
  --trial-id resolution-smoke-001 \
  --hidden-verifier \
  --inject-search-resolution
```

不传 `--skill-config` 时，这种模式默认写到：

```text
benchmark/skillLearnBench/result/no_skill_resolution/<task>/<instance>/<trial-id>/
```

resolution 查询会使用当前 `ov.conf` 里的 User API key，并把当前 instance 映射出来的
peer id 传给 OpenViking。服务端收到 `peer_ids` 后只检索该 peer 下的 memory，不混入普通
user memory，从而保持每道题、每个 instance 的评测隔离。

如果你已经在 `benchmark/skillLearnBench/scripts` 目录，并且 shell 已经激活当前
uv 环境，也可以直接使用：

```bash
python3 run_vikingbot_task.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1
```

## 全量跑 SkillLearnBench

全量 no-skill baseline 可以用 suite runner：

```bash
python3 run_vikingbot_suite.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --suite-id no-skill-rerun-test-202606241431 \
  --skill-config no_skill_no_resolution_2 \
  --parallel 2 \
  --hidden-verifier \
  --skip-existing
```

如果要批量跑 no_skill 并把所有已产生的轨迹导入 OpenViking，可以在 suite
runner 上同样加 `--commit-trajectory-memory`。suite 会把这个开关透传给每个
`run_vikingbot_task.py` 子进程：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_suite.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --suite-id vikingbot-memory-$(date +%Y%m%d%H%M%S) \
  --hidden-verifier \
  --commit-trajectory-memory
```

如果要把已有 suite 的 no_skill 轨迹批量导入 OpenViking，可以复用相同的 `--suite-id`，
并加 `--skip-existing --commit-existing-trajectory-memory`。这个命令不会重跑 Docker
和 bot，只会读取已有 trial 里的 trajectory 并 commit：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_suite.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --suite-id vikingbot-hidden-strict-20260617 \
  --hidden-verifier \
  --skip-existing \
  --commit-trajectory-memory \
  --commit-existing-trajectory-memory \
  --wait-memory-task
```

导入完成后，可以跑 resolution 注入评测：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_suite.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --suite-id vikingbot-resolution-$(date +%Y%m%d%H%M%S) \
  --hidden-verifier \
  --inject-search-resolution
```

如果本地网络访问 Debian/Ubuntu/PyPI/Maven 较慢，可以加镜像参数和并发数。下面是一个
更接近完整本地评测的例子：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_suite.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --suite-id vikingbot-resolution-$(date +%Y%m%d%H%M%S) \
  --hidden-verifier \
  --inject-search-resolution \
  --commit-trajectory-memory \
  --parallel 5 \
  --tool-timeout 600 \
  --apt-mirror http://mirrors.tuna.tsinghua.edu.cn/debian \
  --apt-security-mirror http://mirrors.tuna.tsinghua.edu.cn/debian-security \
  --ubuntu-apt-mirror http://mirrors.tuna.tsinghua.edu.cn/ubuntu \
  --ubuntu-apt-security-mirror http://mirrors.tuna.tsinghua.edu.cn/ubuntu \
  --ubuntu-ports-apt-mirror http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports \
  --maven-mirror https://maven.aliyun.com/repository/public \
  --pip-index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  --uv-index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

先 dry-run 只枚举 100 个 instance，不实际调用 Docker/LLM：

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_suite.py \
  --skilllearnbench-root /Users/bytedance/work/space/test/SkillLearnBench \
  --suite-id vikingbot-full-dryrun \
  --dry-run
```

结果汇总会写到：

```text
benchmark/skillLearnBench/result/suites/<suite-id>/
  manifest.json
  tasks.txt
  results.jsonl
  summary.json
  summary.csv
  logs/
```

每道题的原始 trial 仍然写到：

```text
benchmark/skillLearnBench/result/no_skill/<task>/<instance>/<suite-id>/
```

`github-repo-analytics` 的 5 个 instance 需要 `GH_TOKEN`。如果本地没有设置，
runner 仍会运行并记录 warning，但这些题大概率会因为缺少 GitHub token 而失败。

## 参数说明

### 题目选择

`--skilllearnbench-root`

SkillLearnBench 仓库路径。runner 会在这个目录下查找题目的 `Dockerfile`、
`instruction.md` 和 `tests/`。

```bash
--skilllearnbench-root /private/tmp/SkillLearnBench
```

`--task-id`

要运行的题目 ID，格式通常是 `<task>/<subtask>`。

```bash
--task-id anthropic-poster-design/anthropic-poster-design-1
```

不传时默认跑 `anthropic-poster-design/anthropic-poster-design-1`。

### OpenViking 配置

`--config`

指定要使用的 OpenViking `ov.conf` 路径。

```bash
--config /path/to/ov.conf
```

不传时按 OpenViking 默认链路解析：

```text
OPENVIKING_CONFIG_FILE
~/.openviking/ov.conf
```

这个参数只是指定配置文件路径，不会单独覆盖 model、API key、API base 或 provider。
这些值都应该配置在 `ov.conf` 里。

### Skill 相关

`--skill-source`

指定要注入/评测的 SkillLearnBench skill 目录。

```bash
--skill-source /private/tmp/SkillLearnBench/output/skill_generation_results/human_authored/court-form-filling
```

`--skill-config`

指定结果目录里的 skill 配置名。默认规则是：

- 如果传了 `--skill-source`，使用 skill 目录名。
- 如果没有传 `--skill-source`，且没有开启 `--inject-search-resolution`，使用 `no_skill`。
- 如果没有传 `--skill-source`，但开启了 `--inject-search-resolution`，使用
  `no_skill_resolution`。

结果路径中的第一层就是这个值：

```text
benchmark/skillLearnBench/result/<skill-config>/<task>/<subtask>/<trial-id>/
```

### 输出控制

`--output-root`

指定结果根目录。默认是：

```text
benchmark/skillLearnBench/result
```

`--trial-id`

指定本次运行 ID。不传会自动生成时间戳。调试时建议传固定值，方便反复查看同一路径。

```bash
--trial-id local-smoke-001
```

### 运行限制

`--max-iterations`

VikingBot agent 最大循环轮数，默认 `30`。复杂任务可以调大，例如 `60`。

`--tool-timeout`

单次 Docker-backed `exec` 工具调用的超时时间，单位秒，默认 `300`。
如果题目需要安装依赖、跑长脚本、生成视频或处理大文件，可以调大。

`--docker-build-timeout`

Docker build 超时时间，单位秒，默认 `1800`。

`--verifier-timeout`

执行 `/tests/test.sh` verifier 的超时时间，单位秒，默认 `1800`。

### Suite 并发

`--parallel`

只在 `run_vikingbot_suite.py` 中使用，表示同时跑多少个 instance，默认 `1`。例如
`--parallel 5` 会最多同时启动 5 个单题子进程。并发越高越快，但 Docker build、pip/apt
下载、LLM 调用和 OpenViking memory commit 都会同时发生；本地资源紧张时建议调低。

### 网络镜像

这些参数在单题 runner 和 suite runner 中都可用；suite runner 会透传给每个单题子进程。

`--apt-mirror`

替换 Debian apt 源里的 `http://deb.debian.org/debian`。

`--apt-security-mirror`

替换 Debian security apt 源；不传时跟随 `--apt-mirror`。

`--ubuntu-apt-mirror`

替换 Ubuntu `archive.ubuntu.com/ubuntu` apt 源，适合 `linux/amd64` Ubuntu 镜像。

`--ubuntu-apt-security-mirror`

替换 Ubuntu security apt 源；不传时跟随 `--ubuntu-apt-mirror`。

`--ubuntu-ports-apt-mirror`

替换 Ubuntu `ports.ubuntu.com/ubuntu-ports` apt 源，适合 Apple Silicon Docker 常见的
`linux/arm64` Ubuntu 镜像。不传时会跟随 `--ubuntu-apt-mirror`。

`--maven-mirror`

在 Docker build 阶段写入 `/root/.m2/settings.xml`，给 Maven 配置 `external:*` mirror。

`--pip-index-url`

在 Docker build 早期写入 `/etc/pip.conf`，并设置 `PIP_INDEX_URL`、`UV_INDEX_URL` 和
`UV_DEFAULT_INDEX`，让 build、bot 工具调用和 verifier 里的 pip/uv 下载都尽量走同一个镜像。

`--pip-extra-index-url`

写入 pip 的 `extra-index-url`，适合需要额外 wheel 源的任务。

`--uv-index-url`

显式设置 `UV_INDEX_URL` 和 `UV_DEFAULT_INDEX`。如果没有传该参数但传了 `--pip-index-url`，
uv 默认也会使用同一个 index。

`--uv-extra-index-url`

设置 `UV_EXTRA_INDEX_URL`。

### 调试和清理

`--dry-run`

只解析题目路径、Dockerfile、trial 路径等信息并打印 plan，不构建 Docker、
不启动 VikingBot、不跑 verifier。

`--preflight`

检查本地运行条件，包括 Docker CLI/daemon、VikingBot runtime 和 `ov.conf`
是否能正常加载。不会正式运行题目。

`--keep-container`

运行结束后保留题目 Docker 容器，方便失败后手动进入容器排查。

`--remove-image`

运行结束后删除本次构建出来的 Docker image。默认只删除 container，不删除 image。

### OpenViking 记忆导入

`--commit-trajectory-memory`

只要单题执行产生了 `agent/vikingbot-trajectory.json`，就把它转成 OpenViking session
消息并执行 commit。verifier `PASS` 和 `FAIL` 的轨迹都会提交。

`--commit-trajectory-memory-on-fail`

兼容旧命令的保留参数。当前只要开启 `--commit-trajectory-memory`，失败轨迹也会提交。

`--commit-existing-trajectory-memory`

只导入已有 trial 的 `agent/vikingbot-trajectory.json` 并退出，不构建 Docker、不运行
VikingBot、不跑 verifier。必须和 `--commit-trajectory-memory` 一起使用，并且通常要配合
固定的 `--trial-id` 或 suite 的 `--suite-id --skip-existing`。

`--memory-session-id`

指定导入 OpenViking 时使用的 session id。默认根据 `skill_config/task/subtask/trial-id`
生成，例如：

```text
slb-no_skill-anthropic-poster-design-anthropic-poster-design-1-memory-debug-001
```

`--wait-memory-task`

commit 后等待 OpenViking 后台记忆抽取 task 完成。单题调试时推荐开启；全量跑时会明显变慢。

`--memory-task-timeout`

配合 `--wait-memory-task` 使用，单位秒，默认 `1800`。

### OpenViking Resolution 注入

`--inject-search-resolution`

在 VikingBot 做题前调用 OpenViking `/api/v1/search/resolution`，把返回的 Query Resolution
Pack 注入 bot system context。这个模式仍然是不传 SkillLearnBench skill 的 no-skill bot，
只是多了一段来自 OpenViking memory/skill retrieval 的前置上下文。默认结果目录名是
`no_skill_resolution`。

`--resolution-agent-space`

传给 search/resolution 的 `agent_space`，默认 `default`。如果 OpenViking server 里有按
agent space 隔离的 agent experience、tool memory、skill memory，可以用这个参数指定。

`--resolution-include-debug`

把 search/resolution 的 debug payload 一起保存到
`agent/openviking-search-resolution.json`，包括 retrieval queries、raw candidates 和步骤耗时。
调试有用，但文件会更大。

`--resolution-user-memory-limit`

search/resolution 从当前 instance peer memory 中最多选多少条 user memory，默认 `8`。

`--resolution-experiences-limit`

最多选多少条 agent experience，默认 `5`。

`--resolution-tools-memory-limit`

最多选多少条 tool guidance memory，默认 `5`。

`--resolution-skills-limit`

最多选多少条 skill，默认 `5`。

`--resolution-skills-memory-limit`

最多选多少条 skill memory，默认 `5`。

`--resolution-trajectory-grounding-limit`

最多选多少条 trajectory grounding，默认 `2`。

`--resolution-pack-max-tokens`

Query Resolution Pack 的目标 token 上限，默认 `6000`。runner 注入前还会做字符级截断，
避免 system context 过长。

`--resolution-skill-content-mode`

控制 skill 内容读取方式，可选 `auto/full/summary/link_only`，默认 `auto`。

`--no-resolution-trajectory-grounding`

关闭 search/resolution 里的 trajectory grounding。

`--allow-resolution-failure`

search/resolution 失败时仍继续跑 VikingBot，只是不注入上下文。默认不开启；默认行为是记录
`search_resolution_error`，不运行 bot，最后 verifier 会失败。

## 配置来源

runner 只使用 OpenViking 的 `ov.conf` 配置链路。

如果传了 `--config`，会使用指定的 `ov.conf`：

```bash
.venv/bin/python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /private/tmp/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1 \
  --config /path/to/ov.conf
```

如果不传 `--config`，配置路径解析顺序和 OpenViking server 保持一致：

```text
OPENVIKING_CONFIG_FILE
~/.openviking/ov.conf
```

这里没有单独的 `--model`、`--api-key-env`、`--api-base`、`--provider` 覆盖参数。
模型、API key、API base、provider 都应该放在 `ov.conf` 里，保证 benchmark
和 OpenViking/VikingBot 平时运行使用同一套 provider 配置。

## 哪些东西跑在哪里

```text
宿主机 OpenViking 仓库
  run_vikingbot_task.py
  VikingBot AgentLoop
  Docker-backed read_file/write_file/edit_file/list_dir/exec 工具

SkillLearnBench 题目 Docker 容器
  题目文件，通常在 /root 或 /app
  可选注入的 skill 文件
  agent 生成或修改的题目产物
  /tests/test.sh verifier
  /logs/verifier/reward.txt
```

这个设计避免了给每道题目的 Docker 镜像安装 OpenViking/VikingBot。题目 Docker
只需要保留 SkillLearnBench 原始依赖，agent 本体始终在宿主机 OpenViking 仓库里运行。

## 执行链路

单题运行时，runner 大致做这些事：

1. 读取 SkillLearnBench 题目目录、`Dockerfile`、`instruction.md`、`tests/`。
2. 解析题目 Dockerfile 里的 `WORKDIR`，作为容器内默认工作目录。
3. 使用题目自带的 `environment/` 构建 Docker image。
4. 启动题目容器，并把 `tests/` 挂载到 `/tests`，把 trial 结果目录挂载到 `/logs`。
5. 在宿主机启动 VikingBot `AgentLoop`，加载 OpenViking 的 `ov.conf`。
6. 如果开启 `--inject-search-resolution`，先调用 OpenViking `/api/v1/search/resolution`，
   按当前 instance peer 检索 memory，并把 Query Resolution Pack 注入 system context。
7. 注销 VikingBot 默认工具，只注册 Docker-backed 的 `exec/read_file/write_file/edit_file/list_dir`。
8. VikingBot 调工具时，runner 通过 `docker exec` 把操作转发进题目容器。
9. agent 结束后，在同一个容器里执行 `bash /tests/test.sh`。
10. 读取 verifier 写出的 `/logs/verifier/reward.txt`，生成 `result.json`。

## 使用 SkillLearnBench skill

如果要评测某个 SkillLearnBench 生成或人工编写的 skill，可以传 `--skill-source`：

```bash
.venv/bin/python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /private/tmp/SkillLearnBench \
  --task-id court-form-filling/court-form-filling-1 \
  --skill-source /private/tmp/SkillLearnBench/output/skill_generation_results/human_authored/court-form-filling
```

`--skill-source` 可以是：

- 包含多个子 skill 目录的目录，每个子目录里有 `SKILL.md`。
- 一个自身包含 `SKILL.md` 的 skill 目录。
- 一个只包含若干 `.md` 文件的目录，runner 会把每个 markdown 文件转成一个 `SKILL.md`。

## 输出文件

每次运行会写出：

```text
benchmark/skillLearnBench/result/<skill-config>/<task>/<subtask>/<trial-id>/
  preflight.json
  docker-build.stdout.txt
  docker-build.stderr.txt
  agent/vikingbot-trajectory.json
  agent/vikingbot-stdout.txt
  agent/vikingbot-error.txt
  agent/openviking-search-resolution.json
  agent/openviking-search-resolution.md
  verifier/reward.txt
  verifier/stdout.txt
  verifier/stderr.txt
  result.json
```

其中：

- `agent/vikingbot-trajectory.json`：VikingBot 本次执行的消息、工具调用、token 使用等。
- `agent/openviking-search-resolution.json`：开启 `--inject-search-resolution` 时的完整 resolution payload。
- `agent/openviking-search-resolution.md`：实际注入给 bot 的 resolution context。
- `verifier/reward.txt`：SkillLearnBench verifier 写出的 0/1 reward。
- `result.json`：汇总 pass/fail、reward、token usage、工具列表、Docker 信息和 verifier 输出尾部。

## 常用命令

只解析题目和路径，不构建 Docker、不调用模型：

```bash
.venv/bin/python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /private/tmp/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1 \
  --dry-run
```

保留题目容器，方便失败后手动进入容器排查：

```bash
.venv/bin/python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /private/tmp/SkillLearnBench \
  --task-id anthropic-poster-design/anthropic-poster-design-1 \
  --keep-container
```

提高工具执行超时和 agent 最大轮数，适合重任务：

```bash
.venv/bin/python benchmark/skillLearnBench/scripts/run_vikingbot_task.py \
  --skilllearnbench-root /private/tmp/SkillLearnBench \
  --task-id stock-data-visualization/stock-data-visualization-1 \
  --tool-timeout 600 \
  --max-iterations 60
```

## Trajectory Analysis 打分

原始 bot/Docker 评测结束后，可以用 `run_vikingbot_trajectory_eval.py` 复用已有
`agent/vikingbot-trajectory.json` 跑 SkillLearnBench 官方 trajectory judge。这个步骤不会重新
启动 Docker，也不会重新让 VikingBot 做题。

```bash
uv run python benchmark/skillLearnBench/scripts/run_vikingbot_trajectory_eval.py \
  --skilllearnbench-root /private/tmp/SkillLearnBench \
  --suite-id vikingbot-hidden-strict-20260617 \
  --keep-going \
  --skip-existing-reports \
  --subtask-concurrency 10 \
  --subtask-timeout 300
```

Trajectory judge 也使用 OpenViking 的 `ov.conf`。不传 `--config` 时，路径解析顺序仍然是：

```text
OPENVIKING_CONFIG_FILE
~/.openviking/ov.conf
```

常用参数：

- `--suite-id`：读取 `benchmark/skillLearnBench/result/suites/<suite-id>/summary.csv`，找到要打分的 trial。
- `--summary-csv`：直接指定 suite summary CSV；传了它就不依赖 `--suite-id` 的默认位置。
- `--trajectory-root`：trajectory eval 的输出根目录，默认是 `benchmark/skillLearnBench/result/trajectory_eval`。
- `--task`：只导出/打分某个 task 或 subtask，可重复传。
- `--parent-task`：只跑某个 parent task，例如 `financial-analysis`，可重复传。
- `--export-only`：只把 VikingBot trajectory 转成官方 `evaluation_log` 结构，不调用 judge。
- `--keep-going`：某个 instance 打分失败或超时后继续跑后面的 instance。
- `--skip-existing-reports`：已有 `trajectory_evaluation.json` 的 instance 直接跳过，适合断点续跑。
- `--subtask-timeout`：单个 instance 的 judge 超时秒数，默认 `300`。
- `--subtask-concurrency`：并发跑多少个 instance，默认 `1`；例如 `10` 表示最多同时跑 10 个 instance。
- `--model`：调试用的 judge model 覆盖；不传时使用 `ov.conf` 的 `agents.model`。

结果会写到：

```text
benchmark/skillLearnBench/result/trajectory_eval/evaluation_reports/<skill-config>/<task>/<subtask>/trajectory_evaluation.json
benchmark/skillLearnBench/result/trajectory_eval/evaluation_reports/<skill-config>/<task>/<task>-trajectory-results.csv
benchmark/skillLearnBench/result/trajectory_eval/trajectory_summary.json
```

## 本地测试

runner 的单元测试不需要 Docker，也不需要真实模型 key。它覆盖路径解析、preflight、
skill 注入和伪 Docker/伪 agent 下的结果写出逻辑：

```bash
.venv/bin/python -m pytest tests/benchmark/test_skilllearnbench_runner.py -q
```

## 注意事项

- 当前 adapter 会产出可追踪的 `vikingbot-trajectory.json`，trajectory eval 脚本会把它转换成
  SkillLearnBench 官方评分脚本可读的 JSONL。
- `github-repo-analytics` 这类题目会依赖 `GH_TOKEN`；runner 会读取题目 `task.toml`
  里的 `required_env`，并把宿主机上已有的同名环境变量转发进 Docker 容器。
- `fix-security-bug`、`temperature-simulation`、`stock-data-visualization`、
  `video-object-counting` 等重任务可能需要更大的 `--tool-timeout` 和 `--max-iterations`。
- Trajectory judge 是 LLM-as-judge，`trajectory_key_point_recall` 会按 key point 逐条调用模型。
  并发过高可能触发 provider 限流或让单题空输出更多，建议先从 `--subtask-concurrency 5` 或 `10` 试起。
