import argparse
import json

from .registry import release_configuration
from getpass import getpass

from .admin import set_password
from .app import data_dir
from .db import ServiceError, Store
from .protocol import setup_code


def main():
    parser = argparse.ArgumentParser(description="Python HR Daily 本机配对管理")
    parser.add_argument("command", choices=("setup-code", "reopen-setup", "set-admin-password", "release-config"))
    parser.add_argument("--secondary", action="store_true", help="使用 avatar8790 的独立 HR 数据库")
    args = parser.parse_args()
    if args.command == "release-config":
        print(json.dumps(release_configuration(), ensure_ascii=False, indent=2))
        return
    try:
        store = Store(data_dir() / "avatar8790" if args.secondary else data_dir())
        if args.command == "set-admin-password":
            password = getpass("HR admin password: ")
            if password != getpass("Confirm password: "):
                raise ServiceError("两次密码不一致")
            set_password(store, password)
            print("HR 管理员密码已设置")
        else:
            print(setup_code(store, reopen=args.command == "reopen-setup"))
    except ServiceError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()

