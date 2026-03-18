# -*- coding: utf-8 -*-
"""FTL 用工具：PPA 与地址解析。"""

from .common import *

def translate_ppa_to_address(ppa: int) -> tuple:
    """将 PPA 整数解析为 address 6 元组 (channel, chip, die, plane, block, page)，供 Block_Manager 等使用。
    address 仅用于 ppa 与 address 的互转；lpa 不在此处与 address 互转，lpa 经 FTL 映射为 ppa 后再经本函数得到 address。"""
    if ppa < 0:
        return (0, 0, 0, 0, 0, 0)
    page = ppa % PAGE_PER_BLOCK
    ppa //= PAGE_PER_BLOCK
    block = ppa % BLOCK_PER_PLANE
    ppa //= BLOCK_PER_PLANE
    plane = ppa % PLANE_PER_DIE
    ppa //= PLANE_PER_DIE
    die = ppa % DIE_PER_CHIP
    ppa //= DIE_PER_CHIP
    chip = ppa % CHIP_PER_CHANNEL
    channel = ppa // CHIP_PER_CHANNEL
    return (channel, chip, die, plane, block, page)


def translate_address_to_ppa(addr: tuple) -> int:
    """将 address 6 元组 (channel, chip, die, plane, block, page) 编码为 PPA 整数。
    与 translate_ppa_to_address 互逆，使用相同层级与 common 常量。仅用于 ppa 与 address 互转；lpa 不在此处与 address 互转。"""
    if len(addr) < 6:
        return 0
    channel, chip, die, plane, block, page = addr[0], addr[1], addr[2], addr[3], addr[4], addr[5]
    return ((((channel * CHIP_PER_CHANNEL + chip) * DIE_PER_CHIP + die) * PLANE_PER_DIE + plane)
            * BLOCK_PER_PLANE + block) * PAGE_PER_BLOCK + page

def translate_lpa_to_search_address(lpa: int) -> tuple:
    """将 LPA 整数解析为 (channel, chip, die, plane, search_bank) 元组，供 Search_Manager 使用。"""
    if lpa < 0:
        return (0, 0, 0, 0, 0)
    search_bank = lpa % SEARCH_BANK_PER_PLANE
    lpa //= SEARCH_BANK_PER_PLANE
    plane = lpa % PLANE_PER_DIE
    lpa //= PLANE_PER_DIE
    die = lpa % DIE_PER_CHIP
    lpa //= DIE_PER_CHIP
    chip = lpa % CHIP_PER_CHANNEL
    channel = lpa // CHIP_PER_CHANNEL
    return (channel, chip, die, plane, search_bank)

def translate_lha_to_lpa(lha: int) -> int:
    if lha < STATIC_BASE_LHA:
        return lha // SECTOR_PER_PAGE
    else:
        return lha - STATIC_BASE_LHA + STATIC_BASE_LHA // SECTOR_PER_PAGE

def translate_lpa_to_search_bank_id(lpa: int) -> int:
    """返回 LPA 对应的 search_bank 编号，与 translate_lpa_to_search_address 最后一维一致。"""
    return translate_lpa_to_search_address(lpa)[-1]


def translate_lpa_to_compute_address(lpa: int) -> tuple:
    """将 LPA 整数解析为 (channel, chip, die, plane, compute_bank) 元组，供 Compute 使用。"""
    if lpa < 0:
        return (0, 0, 0, 0, 0)
    compute_bank = lpa % COMPUTE_BANK_PER_PLANE
    lpa //= COMPUTE_BANK_PER_PLANE
    plane = lpa % PLANE_PER_DIE
    lpa //= PLANE_PER_DIE
    die = lpa % DIE_PER_CHIP
    lpa //= DIE_PER_CHIP
    chip = lpa % CHIP_PER_CHANNEL
    channel = lpa // CHIP_PER_CHANNEL
    return (channel, chip, die, plane, compute_bank)


def translate_lpa_to_compute_bank_id(lpa: int) -> int:
    """返回 LPA 对应的 compute_bank 编号，与 translate_lpa_to_compute_address 最后一维一致。"""
    return translate_lpa_to_compute_address(lpa)[-1]
