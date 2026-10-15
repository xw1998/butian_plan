"""Extract source-level contracts without executing application modules."""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent


def text(node):
    return ast.unparse(node) if node is not None else ''


def inspect():
    modules = []
    for path in sorted((ROOT / 'butian').rglob('*.py')):
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        routines = []
        def visit(body, prefix=''):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + '.')
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Each routine owns its own branches; nested helpers listed separately.
                    def own(n):
                        yield n
                        for child in ast.iter_child_nodes(n):
                            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                continue
                            yield from own(child)
                    nodes = list(own(node))
                    sql = []
                    for n in nodes:
                        if isinstance(n, ast.Constant) and isinstance(n.value, str):
                            value = n.value.strip()
                            if value.upper().startswith(('SELECT ', 'INSERT ', 'UPDATE ', 'DELETE ', 'BEGIN', 'SAVEPOINT', 'ROLLBACK', 'COMMIT', 'CREATE ')):
                                sql.append(value)
                    routines.append({'name': prefix + node.name, 'line': node.lineno, 'end_line': node.end_lineno,
                        'signature': text(node.args), 'returns_annotation': text(node.returns),
                        'doc': ast.get_docstring(node),
                        'guards': [{'line': n.lineno, 'condition': text(n.test)} for n in nodes if isinstance(n, (ast.If, ast.While, ast.Assert))],
                        'iterations': [{'line': n.lineno, 'target': text(n.target), 'iterable': text(n.iter)} for n in nodes if isinstance(n, (ast.For, ast.AsyncFor))],
                        'raises': [{'line': n.lineno, 'expression': text(n.exc)} for n in nodes if isinstance(n, ast.Raise)],
                        'returns': [{'line': n.lineno, 'expression': text(n.value)} for n in nodes if isinstance(n, ast.Return)],
                        'handlers': [{'line': n.lineno, 'type': text(n.type)} for n in nodes if isinstance(n, ast.ExceptHandler)],
                        'calls': sorted(set(text(n.func) for n in nodes if isinstance(n, ast.Call))),
                        'sql': list(dict.fromkeys(sql))})
                    visit(node.body, prefix + node.name + '.')
        visit(tree.body)
        modules.append({'path': path.relative_to(ROOT).as_posix(), 'lines': len(source.splitlines()),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'doc': ast.get_docstring(tree),
            'routines': routines})
    target = OUT / 'implementation-ledger.json'
    target.write_text(json.dumps({'method': 'Python AST static extraction; not dynamic coverage or a semantic proof', 'modules': modules}, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 补天逐模块、逐算法、逐分支实现附录', '',
        '本附录从当前源码AST提取。函数签名、条件、返回、异常、循环、SQL与调用名均可回到行号复核；它不将静态存在视为运行覆盖或人工语义审阅。赋值顺序、上下文管理器、动态派发和宿主行为仍以源码为准。完整源码留在仓库，散列用于识别本附录对应的实现。已核验核心路径的人工作用、事务与失败语义见《核心实现语义说明.md》；其他模块仍需逐项人工确认。', '']
    for module in modules:
        lines += ['## ' + module['path'], '', f"源码 SHA-256：`{module['sha256']}`；{module['lines']}行。", '', module['doc'] or '模块级说明未提供，以下记录实际接口与控制分支。', '']
        for routine in module['routines']:
            lines += [f"### {routine['name']}（L{routine['line']}—L{routine['end_line']}）", '',
                '```python', routine['name'] + '(' + routine['signature'] + ')' + (' -> ' + routine['returns_annotation'] if routine['returns_annotation'] else ''), '```', '',
                routine['doc'] or '无函数文档字符串；以下为源码实际控制逻辑。', '']
            for label, key in [('条件与断言', 'guards'), ('循环', 'iterations'), ('显式异常', 'raises'), ('异常处理', 'handlers'), ('返回表达式', 'returns')]:
                entries = routine[key]
                if entries:
                    lines += ['**' + label + '**', '']
                    for item in entries:
                        expression = item.get('condition', item.get('expression', item.get('type', '')))
                        if key == 'iterations':
                            expression = item['target'] + ' in ' + item['iterable']
                        lines.append(f"- L{item['line']}: `{expression.replace(chr(10), ' ')}`")
                    lines.append('')
            if routine['sql']:
                lines += ['**事务与数据操作（静态SQL字面量）**', '']
                for sql in routine['sql']:
                    lines += ['```sql', sql, '```', '']
            lines += ['**调用依赖**：' + ('、'.join('`' + call + '`' for call in routine['calls']) or '无显式函数调用') + '。', '']
    (OUT / '逐模块算法与逻辑附录.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'modules': len(modules), 'routines': sum(len(m['routines']) for m in modules), 'ledger': str(target)}, ensure_ascii=False))
    for module in modules:
        print(module['path'], module['lines'], len(module['routines']), (module['doc'] or '').split('\n')[0][:120])


if __name__ == '__main__':
    inspect()
