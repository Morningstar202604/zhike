"""执行器策略引擎：路径白名单、命令白名单、体积上限、拒绝规则。

这是控制电脑的安全边界，按"默认拒绝"设计：
- 路径必须落在 allow_roots 内，且不命中 deny_globs
- 命令必须命中 allow_binaries，且参数不命中 deny_args_patterns
- 任何不确定的情况一律拒绝
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class PolicyError(Exception):
    """策略拒绝。message 会原样回给调用方，不能含敏感路径细节以外的内容。"""


DEFAULT_POLICY: dict = {
    "version": 1,
    "fs": {
        "allow_roots": [],
        "deny_globs": [
            # 凭据与私钥
            "**/.ssh/**", "**/.aws/**", "**/.gnupg/**", "**/.kube/**",
            "**/.git/config", "**/.env", "**/.env.*",
            "**/*.pem", "**/*.key", "**/*.pfx", "**/*.p12", "**/id_rsa*", "**/id_ed25519*",
            # 浏览器数据（Cookie / 保存的密码）
            "**/Cookies/**", "**/Login Data*", "**/Local State",
            # 系统目录
            "**/Windows/System32/**", "**/Program Files/**", "**/Program Files (x86)/**",
            # 文件名含敏感词的兜底
            "**/*password*", "**/*secret*", "**/*credential*",
        ],
        "max_read_bytes": 1048576,
        "max_write_bytes": 10485760,
        "max_list_entries": 2000,
        "follow_symlinks": False,
    },
    "shell": {
        # 默认白名单只放「读取/查看」类命令，绝不放解释器：
        # python -c / node -e 等于任意代码执行，能绕过 allow_roots 读任意文件（已复现）。
        # 需要跑脚本时用显式脚本文件 + 管理员审批，不要把解释器加回来。
        # 只用 shell=False 能真正执行的命令（dir/type 是 shell 内建，列进来也跑不了）
        "allow_binaries": ["git", "ls", "cat", "echo", "pwd",
                           "grep", "findstr", "head", "tail", "wc"],
        "deny_args_patterns": [
            r"rm\s+-rf\s+/", r"format\s+[a-zA-Z]:", r"del\s+/f\s+/s\s+/q",
            r"shutdown", r"reboot", r"mkfs", r"dd\s+if=",
        ],
        "timeout_s": 120,
        "max_output_bytes": 262144,
        # 即使管理员把解释器加进 allow_binaries，这些内联执行参数也一律拒绝。
        # 按「可执行文件 → 禁止参数」配置：grep -c（计数）不该被 git -c（执行别名）连坐。
        "deny_inline_flags": {
            "python": ["-c", "-m"],
            "python3": ["-c", "-m"],
            "node": ["-e", "--eval", "--require", "--import"],
            "npm": ["exec", "x"],
            "npx": [],
            "powershell": ["-command", "-encodedcommand", "-c"],
            "pwsh": ["-command", "-encodedcommand", "-c"],
            "cmd": ["/c", "/k"],
            "sh": ["-c"],
            "bash": ["-c"],
            "git": ["-c", "--exec-path", "--config-env", "--upload-pack",
                    "--receive-pack", "--open-files-in-pager"],
        },
        "env_allowlist": ["PATH", "HOME", "LANG", "LC_ALL", "TZ", "TEMP", "TMP",
                          "SystemRoot", "COMSPEC", "PATHEXT", "USERPROFILE"],
    },
    "proc": {
        "allow_kill": False,
        "protected_names": ["system", "system idle process", "csrss.exe", "wininit.exe",
                            "winlogon.exe", "services.exe", "lsass.exe", "smss.exe",
                            "explorer.exe", "svchost.exe"],
    },
    "ui": {"enabled": False},
}


@dataclass
class Policy:
    data: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_POLICY)))

    # ---------- 文件路径 ----------

    def _roots(self) -> list[Path]:
        raw = self.data.get("fs", {}).get("allow_roots") or []
        return [Path(os.path.realpath(str(r))) for r in raw if r]

    def _deny_globs(self) -> list[str]:
        return self.data.get("fs", {}).get("deny_globs") or []

    def _norm(self, path: str) -> str:
        """规范化：展开 ~、绝对化、realpath、Windows 上统一大小写。"""
        expanded = os.path.expandvars(os.path.expanduser(str(path)))
        absolute = os.path.abspath(expanded)
        resolved = os.path.realpath(absolute)
        return os.path.normcase(resolved)

    def check_path(self, raw_path: str, *, for_write: bool = False) -> str:
        """校验路径并返回可用的绝对路径。不合规抛 PolicyError。"""
        if not raw_path or not str(raw_path).strip():
            raise PolicyError("路径不能为空")

        s = str(raw_path)
        if "\x00" in s:
            raise PolicyError("路径含空字节")
        if "\n" in s or "\r" in s or "\t" in s:
            raise PolicyError("路径含控制字符")

        roots = self._roots()
        if not roots:
            raise PolicyError("未配置 allow_roots，执行器拒绝一切文件访问")

        resolved = self._norm(s)

        # Windows 设备名/ADS 防护：盘符后的冒号只允许出现在位置 1
        if os.name == "nt":
            body = resolved[2:] if len(resolved) > 1 and resolved[1] == ":" else resolved
            if ":" in body:
                raise PolicyError("路径含备用数据流（ADS）")
            base = os.path.basename(resolved).upper()
            if re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", base):
                raise PolicyError("路径使用了系统保留名")

        # 必须落在某个 allow_root 内（用 commonpath，不用字符串前缀）
        inside = False
        for root in roots:
            try:
                if os.path.commonpath([resolved, os.path.normcase(str(root))]) == os.path.normcase(str(root)):
                    inside = True
                    break
            except ValueError:
                continue
        if not inside:
            raise PolicyError("路径不在允许范围内")

        # deny_globs：按路径段匹配，而不是整串通配。
        # 早期用 fnmatch 整串匹配 + "去前后缀按两段匹配" 的兜底，会把
        # <tmp>/work/hello.txt 里的 "work/hello.txt" 误判成命中 **/.git/config 之类规则。
        # 正确做法：把 glob 按 / 拆成段，在路径段序列里做子序列匹配。
        norm_for_glob = resolved.replace("\\", "/")
        parts = [p for p in norm_for_glob.split("/") if p]
        for pattern in self._deny_globs():
            if self._glob_matches_parts(parts, pattern):
                raise PolicyError("路径命中禁止规则")

        # 符号链接：不允许跟随（避免 link 指向 allow_roots 外）
        if not self.data.get("fs", {}).get("follow_symlinks", False):
            probe = resolved
            # 目标不存在时检查最近的已存在祖先
            while probe and not os.path.exists(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            if probe and os.path.islink(probe):
                raise PolicyError("拒绝操作符号链接")

        if for_write and os.path.exists(resolved) and os.path.isdir(resolved):
            raise PolicyError("目标是目录，不能作为写入文件")

        return resolved

    @staticmethod
    def _glob_matches_parts(parts: list[str], pattern: str) -> bool:
        """按路径段匹配 glob（支持 ** 跨多段）。用于 deny_globs。

        例：pattern="**/.ssh/**"，路径 ["home","u",".ssh","id_rsa"] → 命中
            pattern="**/.env"，路径 ["work",".env"] → 命中
            pattern="**/.env"，路径 ["work","main.py"] → 不命中
        """
        pat_parts = [p for p in pattern.replace("\\", "/").split("/") if p]
        if not pat_parts:
            return False
        lowered = [p.lower() for p in parts]
        norm_pat = [p.lower() for p in pat_parts]

        def match_from(pi: int, si: int) -> bool:
            if pi == len(norm_pat):
                return True
            seg = norm_pat[pi]
            if seg == "**":
                # ** 匹配 0 段或多段
                if match_from(pi + 1, si):
                    return True
                for k in range(si, len(lowered)):
                    if match_from(pi + 1, k + 1):
                        return True
                return False
            if si >= len(lowered):
                return False
            if fnmatch.fnmatchcase(lowered[si], seg):
                return match_from(pi + 1, si + 1)
            return False

        # 允许模式从路径任意一段开始（**/xxx 已显式表达；无前缀模式也按后缀匹配）
        if match_from(0, 0):
            return True
        for start in range(1, len(lowered)):
            if match_from(0, start):
                return True
        return False

    def max_read(self) -> int:
        return int(self.data.get("fs", {}).get("max_read_bytes", 1048576))

    def max_write(self) -> int:
        return int(self.data.get("fs", {}).get("max_write_bytes", 10485760))

    def max_list(self) -> int:
        return int(self.data.get("fs", {}).get("max_list_entries", 2000))

    # ---------- 命令 ----------

    def check_command(self, argv: list[str], cwd: str | None) -> tuple[list[str], str]:
        """校验命令，返回 (argv, 校验后的 cwd)。"""
        if not argv:
            raise PolicyError("命令不能为空")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise PolicyError("argv 必须是字符串数组")

        shell_cfg = self.data.get("shell", {})
        allow = {b.lower() for b in shell_cfg.get("allow_binaries", [])}
        executable = os.path.basename(argv[0]).lower()
        for ext in (".exe", ".cmd", ".bat", ".com"):
            if executable.endswith(ext):
                executable = executable[: -len(ext)]
        if executable not in allow:
            raise PolicyError(f"命令不在白名单：{executable}")

        # argv 里不允许控制字符：防止把多行脚本塞进单个参数
        for arg in argv:
            if any(ch in (chr(0), chr(13)) or ch == chr(10) for ch in arg):
                raise PolicyError("命令参数含控制字符")

        inline_map = shell_cfg.get("deny_inline_flags", {}) or {}
        inline = {f.lower() for f in inline_map.get(executable, [])}
        for arg in argv[1:]:
            low = arg.lower()
            if low in inline or any(low.startswith(f + "=") for f in inline if f.startswith("--")):
                raise PolicyError(f"拒绝内联执行参数：{arg}")

        joined = " ".join(argv)
        for pattern in shell_cfg.get("deny_args_patterns", []):
            if re.search(pattern, joined, re.IGNORECASE):
                raise PolicyError("命令命中禁止规则")

        # 可执行文件必须能从 PATH 解析到；绝对路径必须与 PATH 解析结果一致，
        # 防止把改名后的恶意二进制放进工作目录再以 argv[0] 绝对路径调用。
        import shutil as _shutil
        resolved = _shutil.which(argv[0])
        if not resolved:
            raise PolicyError(f"命令不存在或不可执行：{argv[0]}")
        if os.path.isabs(argv[0]) and os.path.normcase(os.path.realpath(argv[0])) != os.path.normcase(os.path.realpath(resolved)):
            raise PolicyError("可执行文件路径与 PATH 解析结果不一致")

        workdir = ""
        if cwd:
            workdir = self.check_path(cwd)
            if not os.path.isdir(workdir):
                raise PolicyError("工作目录不存在")
        return argv, workdir

    def command_timeout(self) -> int:
        return int(self.data.get("shell", {}).get("timeout_s", 120))

    def max_output(self) -> int:
        return int(self.data.get("shell", {}).get("max_output_bytes", 262144))

    def safe_env(self) -> dict[str, str]:
        """只透传白名单环境变量；任何 *_TOKEN/*_KEY/*_SECRET 都不出现在子进程里。"""
        allow = set(self.data.get("shell", {}).get("env_allowlist", []))
        out = {}
        for k, v in os.environ.items():
            if k in allow:
                out[k] = v
        # 双保险：即使用户把敏感变量加进白名单，也剔除
        for k in list(out):
            if re.search(r"(TOKEN|KEY|SECRET|PASSWORD)", k, re.IGNORECASE):
                out.pop(k, None)
        return out

    # ---------- 进程 ----------

    def check_kill(self, pid: int, name: str = "") -> None:
        proc_cfg = self.data.get("proc", {})
        if not proc_cfg.get("allow_kill", False):
            raise PolicyError("进程结束能力未启用")
        protected = {p.lower() for p in proc_cfg.get("protected_names", [])}
        if (name or "").lower() in protected:
            raise PolicyError("拒绝结束受保护的系统进程")
        if pid in (0, 1, 4):
            raise PolicyError("拒绝结束系统进程")

    def ui_enabled(self) -> bool:
        return bool(self.data.get("ui", {}).get("enabled", False))


def load_policy(path: str | None = None) -> Policy:
    """从 JSON 文件加载策略；文件不存在时用默认策略（allow_roots 为空 = 全拒绝）。"""
    if not path:
        return Policy()
    p = Path(path)
    if not p.exists():
        return Policy()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PolicyError(f"策略文件无法解析：{type(e).__name__}") from e
    merged = json.loads(json.dumps(DEFAULT_POLICY))
    for section, values in (data or {}).items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return Policy(merged)
