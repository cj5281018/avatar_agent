import argparse
import re
import secrets
from getpass import getpass

from .db import Store, data_dir, password_hash, uid


def main():
    parser = argparse.ArgumentParser(description="Python Avatar 本地试点")
    parser.add_argument("command", choices=("init-demo", "add-demo-user"))
    parser.add_argument("--handle")
    parser.add_argument("--name")
    args = parser.parse_args()
    if args.command == "add-demo-user":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", args.handle or "") or not (args.name or "").strip():
            parser.error("请提供有效的 --handle 和 --name")
        password = getpass("Avatar password: ")
        if not 12 <= len(password) <= 512 or password != getpass("Confirm password: "):
            parser.error("密码必须是 12–512 字符且两次输入一致")
        salt = secrets.token_hex(16)
        with Store(data_dir()).open() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users WHERE handle=?", (args.handle,)).fetchone():
                parser.error("Avatar 账号已存在")
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                       (uid("usr"), args.handle, args.name.strip(), "member", salt, password_hash(password, salt)))
        print("Avatar 模拟员工已创建")
        return
    users = Store(data_dir()).seed_demo()
    if not users:
        print("模拟账户已存在，本次未修改密码。")
    for handle, password in users:
        print(f"{handle}: {password}")


if __name__ == "__main__":
    main()

