"""Local terminal credential setup. Never prints or accepts secrets in argv."""
import argparse
import getpass
import os
from pathlib import Path
from .runtime.store import Store


def main():
    parser = argparse.ArgumentParser(description='配置工作台成员访问口令（仅限本机终端）')
    parser.add_argument('--member', required=True, help='成员标识')
    args = parser.parse_args()
    home = Path(os.environ.get('BUTIAN_HOME', Path.home() / '.butian'))
    if not (home / 'team.db').is_file():
        parser.error('尚未找到运行库，请先初始化项目。')
    store = Store(home / 'team.db')
    try:
        member = store.db.execute('SELECT name FROM members WHERE member_id=?', (args.member,)).fetchone()
        if not member:
            parser.error('未找到该数字人。')
        print('配置访问身份：' + member['name'])
        password = getpass.getpass('新访问口令（至少12位，不显示输入）：')
        if len(password) < 12:
            parser.error('口令至少12位。')
        if password != getpass.getpass('再次输入访问口令：'):
            parser.error('两次输入不一致，未更改口令。')
        store.set_member_token(args.member, password)
        print('访问口令已设置，请回到网页重新加载成员列表。')
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
