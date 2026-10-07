"""回访归因后端命令行入口。

无参数运行时保留项目初始的演示输出；带参数时进入完整 CLI
（详见 python3 run_cli.py --help）。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from task_domain_006.core import Record, stable_summary
from task_domain_006.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print(stable_summary(Record("demo", "v1", "draft", "operator")))
        print("提示: 使用 'python3 run_cli.py --help' 查看回访归因后端命令", file=sys.stderr)
    else:
        sys.exit(main(sys.argv[1:]))
