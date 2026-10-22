"""Export an existing Ctrip profile, or log in manually, for use on another host."""

import argparse
from pathlib import Path

from .ctrip import browser, save_state


def main():
    parser = argparse.ArgumentParser(description="携程登录/会话导出；不会读取监控库或发送消息")
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--login", action="store_true", help="打开浏览器手动登录，再按 Enter 保存")
    args = parser.parse_args()
    if not args.login and not (args.profile_dir / "Default").is_dir():
        raise FileNotFoundError("没有已保存的浏览器资料，请指定已有目录或添加 --login")
    with browser(
        args.profile_dir.resolve(), None, headless=not args.login, mobile=False
    ) as context:
        if args.login:
            page = context.new_page()
            page.goto("https://www.ctrip.com/", wait_until="domcontentloaded")
            input("请在浏览器完成携程登录，回到终端按 Enter 保存：")
        save_state(context, args.state_file.resolve())
    print(f"携程会话已保存：{args.state_file.resolve()}（含登录凭证，请私下传输）")


if __name__ == "__main__":
    main()
