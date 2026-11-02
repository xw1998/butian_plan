"""两段式预览令牌：先看清这次操作要写什么，再拿令牌落盘。

批量写（蒸馏 / 抽象）的风险不是“写错一行”，而是“写了一份基于过期记忆的快照”：
宿主先 butian_memory_search 再提交，中间记忆的正文或状态可能已经变了。
预览把“影响集”算成一个指纹，令牌就是该指纹的前缀；提交时重算一次，
不一致就拒绝写库并告诉调用方重新预览，而不是写下去之后再让用户发现对不上。

开关 BUTIAN_REQUIRE_DREAM_PREVIEW 默认关：不设时提交行为与以前完全一致，
只有显式打开才强制要求先预览。
"""
from __future__ import annotations
import os

ENV_REQUIRE = 'BUTIAN_REQUIRE_DREAM_PREVIEW'
TOKEN_PREFIX = 'pt_'


class PreviewStale(ValueError):
    """令牌与当前状态不一致：预览之后对象被改过，旧令牌不能用。"""

    def __init__(self, details):
        super().__init__('preview_stale')
        self.code = 'preview_stale'
        self.details = details


def required_by_env():
    return os.environ.get(ENV_REQUIRE, '').strip().lower() in {'1', 'true', 'yes', 'on'}


def token_for(fingerprint):
    """令牌是影响集指纹的前缀：短到能背下来，又足够避免手抄错行。"""
    return TOKEN_PREFIX + str(fingerprint)[:32]


def matches(token, fingerprint):
    return isinstance(token, str) and token.strip() == token_for(fingerprint)


def require_fresh(token, fingerprint, *, operation, tool, next_arguments):
    """提交前的一致性校验；不一致直接抛出可恢复的 preview_stale。"""
    if matches(token, fingerprint):
        return True
    hint = {'tool': tool,
            'arguments': dict(next_arguments, preview_only=True),
            'why': '预览之后源记忆或提交内容变了：重新预览拿新令牌再提交，旧令牌不能用来写库。'}
    raise PreviewStale({
        'code': 'preview_stale',
        'operation': operation,
        'current': {'preview_token': token_for(fingerprint), 'fingerprint': fingerprint},
        'missing_conditions': ['preview_token_matches_current_state'],
        'reason': '这份预览已经过期：源记忆正文/状态或提交内容与预览时不一致。',
        'next_actions': [hint],
    })
