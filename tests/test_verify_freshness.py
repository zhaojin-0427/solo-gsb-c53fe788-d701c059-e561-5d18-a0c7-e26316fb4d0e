"""交付字幕核验「过期结论」修复的回归测试：结论与生效时间轴绑定。

核验响应与项目详情（及各写路径返回）携带同源 ``timeline_hash``
（生效轴指纹：已采纳 → 提案 → 原值 的 text/起止序列），前端据此决定
已展示的结论作废还是保留：

- 参数重算 / 采纳 / 撤销**改变了生效轴** ⇒ 指纹变化 ⇒ 旧结论必须作废，
  用已选回传稿按新轴重核（典型场景：相接两条字幕 min_gap 40→100，
  提案 1040→1100，按 1040 的「通过」结论不得再展示）；
- 相同参数重提交、修改被 400 拒绝、未影响生效轴的写入 ⇒ 指纹不变 ⇒
  有效结论保留；
- 核验为只读：核验本身不改变指纹，导出不受影响；
- 每次响应都绑定当时的生效轴：并发下迟到的响应携带旧指纹，与当前
  详情指纹不一致，前端据此丢弃，只呈现最近一次有效上传的结果。
"""
import asyncio
import io
import os
import sys

DB = "/tmp/test_verify_freshness.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_PATH"] = DB  # 须在导入 app 之前设置

sys.path.insert(0, "/workspace")

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import main as m  # noqa: E402

m.db.init_db()

# 相接两条字幕：0–1000 / 1000–2000（原间隔 0）
SRT = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
       "2\n00:00:01,000 --> 00:00:02,000\n乙\n")

# 按 min_gap=40 的提案（1040–2040）回传的交付稿
DELIVERY_1040 = ("1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
                 "2\n00:00:01,040 --> 00:00:02,040\n乙\n")


def _upload(name: str, content: str) -> UploadFile:
    return UploadFile(io.BytesIO(content.encode("utf-8")), filename=name)


def _create(srt=SRT, gap=40, tol=200):
    return asyncio.run(m.create_project(
        name="指纹场景", cps_limit=21.0, min_gap_ms=gap, tolerance_ms=tol,
        subtitle=_upload("a.srt", srt), shots_file=None))


def _settings(pid, gap=40, tol=200):
    return m.update_settings(pid, m.SettingsIn(
        cps_limit=21.0, min_gap_ms=gap, tolerance_ms=tol))


def _verify(pid, content=DELIVERY_1040):
    return asyncio.run(m.verify_delivery(pid, _upload("back.srt", content)))


def test_hash_present_and_consistent_everywhere():
    """详情、写路径响应与核验响应携带同源 timeline_hash。"""
    proj = _create()
    pid = proj["id"]
    h0 = proj["timeline_hash"]
    assert h0 and isinstance(h0, str)
    # 详情接口同源
    assert m.get_project(pid)["timeline_hash"] == h0
    # 核验响应与详情一致
    v = _verify(pid)
    assert v["timeline_hash"] == h0
    assert v["project_id"] == pid
    # 各写路径返回的 payload 均携带指纹（前端同步依赖）
    assert _settings(pid, gap=40)["timeline_hash"] == h0
    cues = proj["analysis"]["cues"]
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=1040, new_end_ms=2040))
    assert proj["timeline_hash"]
    proj = m.revert_adoption(pid, cues[1]["uid"])
    assert proj["timeline_hash"] == h0
    print("timeline_hash present & consistent across payloads ok")


def test_settings_change_invalidates_stale_pass():
    """典型场景：min_gap 40→100，提案 1040→1100，旧「通过」结论必须作废。"""
    proj = _create()
    pid = proj["id"]
    cues = proj["analysis"]["cues"]
    assert [c["proposal_start_ms"] for c in cues] == [0, 1040]
    h40 = proj["timeline_hash"]

    # 回传稿按 1040 提案核验：全部通过
    v = _verify(pid)
    assert v["summary"]["pass"] == 2 and v["timeline_hash"] == h40

    # 最小间隔改为 100ms：提案变为 1100，生效轴指纹必须变化
    proj = _settings(pid, gap=100)
    h100 = proj["timeline_hash"]
    assert h100 != h40
    assert proj["analysis"]["cues"][1]["proposal_start_ms"] == 1100
    assert m.get_project(pid)["timeline_hash"] == h100

    # 同一回传稿按新生效轴重核：第二条变为时间不符（1040 vs 1100）
    v2 = _verify(pid)
    assert v2["timeline_hash"] == h100
    s = v2["summary"]
    assert s["pass"] == 1 and s["time_mismatch"] == 1, s
    row = next(r for r in v2["rows"] if r["kind"] == "match"
               and r["text"] == "乙")
    assert row["delta_start_ms"] == -60 and row["delta_end_ms"] == -60
    print("settings change invalidates stale pass (1040 -> 1100) ok")


def test_unchanged_axis_keeps_hash():
    """未改变生效轴的写入：指纹不变，已展示的有效结论保留。"""
    proj = _create()
    pid = proj["id"]
    h0 = proj["timeline_hash"]

    # 相同参数重复提交：指纹不变
    assert _settings(pid, gap=40, tol=200)["timeline_hash"] == h0

    # 修改失败（非法参数 400）：状态未动，指纹不变
    try:
        m.update_settings(pid, m.SettingsIn(
            cps_limit=0, min_gap_ms=40, tolerance_ms=200))
        raise AssertionError("非法参数应 400")
    except HTTPException as exc:
        assert exc.status_code == 400
    assert m.get_project(pid)["timeline_hash"] == h0

    # 采纳提案（采纳值 = 当前生效值）：生效轴不变，指纹不变
    cues = proj["analysis"]["cues"]
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=1040, new_end_ms=2040))
    assert proj["analysis"]["cues"][1]["adopted"]
    assert proj["timeline_hash"] == h0

    # 参数未变时撤销：生效值回落到同一提案，指纹仍不变
    proj = m.revert_adoption(pid, cues[1]["uid"])
    assert proj["timeline_hash"] == h0

    # 采纳后改 min_gap=30：提案变 1030 但采纳 1040 仍合法保留，
    # 生效轴第二条仍是 1040 → 指纹不变，旧结论继续有效
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=1040, new_end_ms=2040))
    proj = _settings(pid, gap=30)
    c2 = proj["analysis"]["cues"][1]
    assert c2["adopted"] and c2["adopted_start_ms"] == 1040
    assert c2["proposal_start_ms"] == 1030
    assert proj["timeline_hash"] == h0
    print("same-params / failed update / axis-preserving writes keep hash ok")
    return pid


def test_axis_changing_writes_bump_hash(pid):
    """采纳、撤销、参数重算一旦改变生效轴，指纹必须变化。"""
    h_adopted = m.get_project(pid)["timeline_hash"]

    # 撤销已保留的采纳：生效值从采纳值 1040 变为新提案 1030 → 指纹变
    cues = m.get_project(pid)["analysis"]["cues"]
    proj = m.revert_adoption(pid, cues[1]["uid"])
    h_1030 = proj["timeline_hash"]
    assert h_1030 != h_adopted
    assert proj["analysis"]["cues"][1]["proposal_start_ms"] == 1030

    # 参数重算改变提案（30→100，提案 1030→1100）：指纹变
    proj = _settings(pid, gap=100)
    h_1100 = proj["timeline_hash"]
    assert h_1100 not in (h_adopted, h_1030)

    # 采纳新提案 1100：生效值不变（提案=采纳值）→ 指纹不变
    cues = proj["analysis"]["cues"]
    proj = m.adopt(pid, m.AdoptIn(cue_uid=cues[1]["uid"],
                                  new_start_ms=1100, new_end_ms=2100))
    assert proj["timeline_hash"] == h_1100

    # 收紧到 120ms：采纳 1100 间隔不足被撤销，生效轴变为提案 1120 → 指纹变
    proj = _settings(pid, gap=120)
    assert proj["analysis"]["cues"][1]["adopted"] is False
    assert proj["analysis"]["cues"][1]["proposal_start_ms"] == 1120
    assert proj["timeline_hash"] != h_1100
    print("axis-changing adopt/revert/settings bump hash ok")


def test_verify_readonly_and_late_response_binding():
    """核验只读且每次响应绑定当时生效轴：迟到响应携带旧指纹可被识别。"""
    proj = _create()
    pid = proj["id"]
    h_before = proj["timeline_hash"]
    doc_before = m.export(pid, fmt="json").body
    srt_before = m.export(pid, fmt="srt").body

    # 旧轴上的核验结果（模拟「迟到响应」）：指纹对应 min_gap=40 的生效轴
    stale = _verify(pid)
    assert stale["timeline_hash"] == h_before
    assert stale["summary"]["pass"] == 2

    # 并发改参数先生效：生效轴变为 min_gap=100 的提案 1100
    proj = _settings(pid, gap=100)
    h_now = proj["timeline_hash"]
    # 迟到响应的指纹与当前详情不一致 → 前端据此丢弃，不得展示旧「通过」
    assert stale["timeline_hash"] != h_now

    # 当前轴上的新核验：指纹与详情一致，结论为时间不符
    fresh = _verify(pid)
    assert fresh["timeline_hash"] == h_now
    assert fresh["summary"]["time_mismatch"] == 1

    # 核验本身只读：指纹未因核验改变；回到 min_gap=40 后导出与核验前
    # 字节级一致（核验未留下任何痕迹）
    assert m.get_project(pid)["timeline_hash"] == h_now
    _settings(pid, gap=40)
    assert m.get_project(pid)["timeline_hash"] == h_before
    assert m.export(pid, fmt="json").body == doc_before
    assert m.export(pid, fmt="srt").body == srt_before
    print("verify read-only + late response carries stale hash ok")


def main():
    test_hash_present_and_consistent_everywhere()
    test_settings_change_invalidates_stale_pass()
    pid = test_unchanged_axis_keeps_hash()
    test_axis_changing_writes_bump_hash(pid)
    test_verify_readonly_and_late_response_binding()
    print("verify freshness regression ok")


if __name__ == "__main__":
    main()
