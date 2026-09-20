# 必要配置确认

下列内容在用户全局 config.json 中存在，最新源码配置未包含。代码仍依赖这些字段；仅恢复下列现有值，不恢复旧 context、task_title 或长度限制。尚未写入正式配置。

- model_gateway：选择协议、连接超时、并行工具调用。
- execution.python_executable/environment_dir/cache_dir：解释器选择、项目依赖与缓存位置。
- execution.output_encodings：保持中文命令输出解码。
- execution.inherit_env/variables：继承已允许的 Git/SSH/代理环境，并将缓存和临时文件留在项目运行目录。
- execution.permissions：保持已确认的现有权限保护，不新增规则。

```json
{
  "model_gateway": {
    "protocol": "openai-chat",
    "connect_timeout": 10,
    "settings": {
      "parallel_tool_calls": true
    }
  },
  "execution": {
    "python_executable": "",
    "environment_dir": "{runtime}/venv",
    "cache_dir": "{runtime}/cache",
    "inherit_env": [
      "PATH",
      "SystemRoot",
      "WINDIR",
      "COMSPEC",
      "PATHEXT",
      "SYSTEMDRIVE",
      "HTTP_PROXY",
      "HTTPS_PROXY",
      "NO_PROXY",
      "PLAYWRIGHT_BROWSERS_PATH",
      "CLAWHUB_WORKDIR",
      "HOME",
      "USERPROFILE",
      "APPDATA",
      "LOCALAPPDATA",
      "HOMEDRIVE",
      "HOMEPATH",
      "XDG_CONFIG_HOME",
      "SSH_AUTH_SOCK",
      "SSH_AGENT_PID",
      "GIT_CONFIG_GLOBAL",
      "GIT_CONFIG_SYSTEM",
      "GIT_SSH",
      "GIT_SSH_COMMAND",
      "NPM_CONFIG_USERCONFIG",
      "npm_config_userconfig"
    ],
    "permissions": {
      "restricted_roles": [
        "worker",
        "manager"
      ],
      "blocked_commands": [
        "kill",
        "pkill",
        "killall",
        "taskkill",
        "tskill",
        "stop-process",
        "spps",
        "shutdown",
        "restart-computer",
        "stop-computer",
        "stop-service",
        "restart-service"
      ],
      "script_extensions": [
        ".py",
        ".pyw",
        ".ps1",
        ".sh",
        ".bat",
        ".cmd",
        ".js",
        ".mjs",
        ".cjs"
      ],
      "blocked_python_calls": [
        "os.kill",
        "os.killpg",
        "signal.pthread_kill",
        "psutil.Process.kill",
        "psutil.Process.terminate",
        "subprocess.Popen.kill",
        "subprocess.Popen.terminate",
        "multiprocessing.Process.kill",
        "multiprocessing.Process.terminate"
      ],
      "command_wrappers": [
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "os.popen",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell"
      ],
      "blocked_script_patterns": [
        "\\bprocess\\s*\\.\\s*kill\\s*\\(",
        "\\b(?:TerminateProcess|NtTerminateProcess|TerminateJobObject)\\s*\\("
      ],
      "blocked_shell_options": [
        "-encodedcommand",
        "-enc",
        "-ec"
      ],
      "background_commands": [
        "start",
        "nohup",
        "setsid",
        "start-process"
      ],
      "require_explicit_commands": true,
      "argv_command_wrappers": [
        "asyncio.create_subprocess_exec"
      ],
      "javascript_modules": [
        "child_process",
        "node:child_process"
      ],
      "javascript_command_wrappers": {
        "exec": "shell",
        "execSync": "shell",
        "spawn": "argv",
        "spawnSync": "argv",
        "execFile": "argv",
        "execFileSync": "argv",
        "fork": "script"
      }
    },
    "variables": {
      "PIP_CACHE_DIR": "{cache}/{project_id}/pip",
      "npm_config_cache": "{cache}/{project_id}/npm",
      "UV_CACHE_DIR": "{cache}/{project_id}/uv",
      "XDG_CACHE_HOME": "{cache}/{project_id}/xdg",
      "TEMP": "{runtime}/tmp",
      "TMP": "{runtime}/tmp",
      "TMPDIR": "{runtime}/tmp",
      "PYTHONNOUSERSITE": "1",
      "PYTHONUTF8": "1",
      "PIP_DISABLE_PIP_VERSION_CHECK": "1"
    },
    "output_encodings": [
      "utf-8-sig",
      "locale"
    ]
  }
}
```
