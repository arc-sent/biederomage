"""Лимит групп на пользователя (MAX_GROUPS_PER_USER)."""

import pytest

import db

TID = 2002


def _account():
    db.ensure_user(TID)
    return db.add_account(TID, "Акк", "vk1.a.fake_token_for_tests_x" + "a" * 30)


def test_limit_blocks_new_group(monkeypatch):
    monkeypatch.setattr(db, "MAX_GROUPS_PER_USER", 3)
    aid = _account()
    for gid in range(1, 4):
        db.add_group(TID, vk_group_id=gid, name=f"G{gid}", account_id=aid)
    with pytest.raises(db.GroupLimitError):
        db.add_group(TID, vk_group_id=4, name="G4", account_id=aid)
    assert db.count_groups(TID) == 3


def test_limit_allows_update_of_existing(monkeypatch):
    monkeypatch.setattr(db, "MAX_GROUPS_PER_USER", 1)
    aid = _account()
    db.add_group(TID, vk_group_id=1, name="G1", account_id=aid)
    db.add_group(TID, vk_group_id=1, name="Новое имя", account_id=aid)  # upsert, не новая
    assert db.get_groups(TID)[0]["name"] == "Новое имя"


def test_limit_frees_after_delete(monkeypatch):
    monkeypatch.setattr(db, "MAX_GROUPS_PER_USER", 1)
    aid = _account()
    db.add_group(TID, vk_group_id=1, name="G1", account_id=aid)
    db.delete_group(db.get_groups(TID)[0]["id"])
    db.add_group(TID, vk_group_id=2, name="G2", account_id=aid)
    assert db.count_groups(TID) == 1


def test_default_limit_is_30():
    assert db.MAX_GROUPS_PER_USER == 30
