#!/usr/bin/env python3.11
"""
用户管理脚本
用法:
  python3.11 manage_users.py passwd <用户名>   # 修改密码
  python3.11 manage_users.py add <用户名>      # 添加用户
  python3.11 manage_users.py del <用户名>      # 删除用户
  python3.11 manage_users.py list              # 列出所有用户
"""

import sys
import json
import getpass
import bcrypt
from pathlib import Path

USERS_FILE = Path(__file__).parent / "users.json"


def load() -> dict:
    with open(USERS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save(config: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"已保存至 {USERS_FILE}")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def prompt_password(username: str) -> str:
    while True:
        pw = getpass.getpass(f"请输入 {username} 的新密码: ")
        if not pw:
            print("密码不能为空，请重试")
            continue
        pw2 = getpass.getpass("再次确认密码: ")
        if pw != pw2:
            print("两次输入不一致，请重试")
            continue
        return pw


def cmd_passwd(username: str):
    config = load()
    if username not in config["users"]:
        print(f"用户 '{username}' 不存在")
        sys.exit(1)
    pw = prompt_password(username)
    config["users"][username] = hash_password(pw)
    save(config)
    print(f"用户 '{username}' 密码已更新")


def cmd_add(username: str):
    config = load()
    if username in config["users"]:
        print(f"用户 '{username}' 已存在")
        sys.exit(1)
    pw = prompt_password(username)
    config["users"][username] = hash_password(pw)
    save(config)
    print(f"用户 '{username}' 已添加")


def cmd_del(username: str):
    config = load()
    if username not in config["users"]:
        print(f"用户 '{username}' 不存在")
        sys.exit(1)
    confirm = input(f"确认删除用户 '{username}'？[y/N] ").strip().lower()
    if confirm != "y":
        print("已取消")
        return
    del config["users"][username]
    save(config)
    print(f"用户 '{username}' 已删除")


def cmd_list():
    config = load()
    users = config["users"]
    if not users:
        print("暂无用户")
        return
    print(f"共 {len(users)} 个用户:")
    for name in sorted(users):
        print(f"  {name}")


def usage():
    print(__doc__.strip())
    sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        usage()

    cmd = args[0]
    if cmd == "list":
        cmd_list()
    elif cmd == "passwd" and len(args) == 2:
        cmd_passwd(args[1])
    elif cmd == "add" and len(args) == 2:
        cmd_add(args[1])
    elif cmd == "del" and len(args) == 2:
        cmd_del(args[1])
    else:
        usage()
