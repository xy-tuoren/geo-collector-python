"""Explicit platform flows; shared capture starts after ``answer_ready``."""

from . import douyin, douyin_direct, toutiao, xiaohe


PLATFORMS = {
    xiaohe.ID: xiaohe,
    douyin.ID: douyin,
    toutiao.ID: toutiao,
    douyin_direct.ID: douyin_direct,
}
PACKAGES = {name: platform.PACKAGE for name, platform in PLATFORMS.items()}

ENTRY_LABELS = {
    "xiaohe": "小荷AI医生APP",
    "douyin": "抖音搜索框（小荷AI小程序）",
    "toutiao": "头条搜索框（小荷AI小程序）",
    "douyin-direct": "抖音小荷小程序",
}
