#!/usr/bin/env python3
"""fmddr — FileMaker DDR（Database Design Report）を AI が読める形に分解するツール。

Python 標準ライブラリのみで動作する（外部ツール・追加インストール不要）。

サブコマンド:
  probe    DDR の種別・バージョン・件数・構成ファイルを表示する
  split    DDR を Markdown ＋ JSON インデックス（index.json / xref.json）に分解する
  nodes    展開せずに、指定パス配下の要素・属性を列挙する
  extract  展開せずに、指定パスの XML を切り出す
  xref     split 済み出力に対して相互参照（どこで使われているか）を引く
  audit    未使用スクリプト・値一覧・レイアウトなどを洗い出す

DDR は UTF-16 の巨大な単一 XML なので、常に iterparse でストリーム処理し、
処理済みの要素を解放しながら進む。数百 MB でもメモリは一定に収まる。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime

VERSION = "1.1.0"
MANIFEST = ".fmddr-manifest.json"

# ---------------------------------------------------------------------------
# 1. 共通ユーティリティ
# ---------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, used: set[str], suffix: str = ".md") -> str:
    """ファイル名として安全な名前を作る。同一ディレクトリ内の衝突も避ける。"""
    base = unicodedata.normalize("NFC", (name or "").strip()) or "（無名）"
    base = _BAD_CHARS.sub("_", base).strip().strip(".") or "_"
    base = base[:110]
    cand = base
    n = 2
    while cand.casefold() in used:
        cand = f"{base}__{n}"
        n += 1
    used.add(cand.casefold())
    return cand + suffix


def text_of(el, default: str = "") -> str:
    """要素のテキストを取り出す（None 安全）。"""
    if el is None:
        return default
    return (el.text or default)


def one_line(s: str, limit: int = 160) -> str:
    """改行を含む文字列を 1 行に潰す（一覧表示用）。"""
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def num(v) -> str:
    """DDR の座標 "22.0000000" を "22" に縮める。"""
    if v is None:
        return "?"
    try:
        f = float(v)
    except ValueError:
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def is_true(v) -> bool:
    return str(v) == "True"


def field_key(el) -> str:
    """<Field table=".." name=".."> を "TO::フィールド名" 形式にする。"""
    name = el.get("name") or ""
    table = el.get("table") or el.get("baseTable") or ""
    return f"{table}::{name}" if table else name


def fence(body: str, lang: str = "") -> list[str]:
    """コードフェンスで囲む。本文が ``` を含む場合は区切りを伸ばす。"""
    delim = "```"
    while delim in body:
        delim += "`"
    return [delim + lang, body.rstrip("\n"), delim]


def eprint(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------------------
# 2. DDR のストリーム読み込み
# ---------------------------------------------------------------------------

# 定義（実体）のパス。同じタグ名でも参照として現れるものと区別するため、
# タグ名ではなく「絶対パス」で判定する。
# 例: Layout 配下の <Script name="..."/> は「参照」、
#     ScriptCatalog 配下の <Script> が「定義」。
DEF_PATTERNS = {
    "basetable": r"^FMPReport/File/BaseTableCatalog/BaseTable$",
    "script": r"^FMPReport/File/ScriptCatalog/(?:Group/)*Script$",
    "layout": r"^FMPReport/File/LayoutCatalog/(?:Group/)*Layout$",
    "valuelist": r"^FMPReport/File/ValueListCatalog/ValueList$",
    "to": r"^FMPReport/File/RelationshipGraph/TableList/Table$",
    "relationship": r"^FMPReport/File/RelationshipGraph/RelationshipList/Relationship$",
    "customfunction": r"^FMPReport/File/CustomFunctionCatalog/CustomFunction$",
    "custommenu": r"^FMPReport/File/CustomMenuCatalog/CustomMenu$",
    "menuset": r"^FMPReport/File/CustomMenuSetCatalog/CustomMenuSet$",
    "account": r"^FMPReport/File/AccountCatalog/Account$",
    "privilegeset": r"^FMPReport/File/PrivilegesCatalog/PrivilegeSet$",
    "extprivilege": r"^FMPReport/File/ExtendedPrivilegeCatalog/ExtendedPrivilege$",
    "fileref": r"^FMPReport/File/ExternalDataSourcesCatalog/FileReference$",
    "theme": r"^FMPReport/File/ThemeCatalog/Theme$",
    "options": r"^FMPReport/File/Options$",
    "authfile": r"^FMPReport/File/AuthFileCatalog$",
    "basedir": r"^FMPReport/File/BaseDirectoryCatalog$",
}
DEF_RE = {k: re.compile(v) for k, v in DEF_PATTERNS.items()}
DEF_TAGS = {
    "BaseTable", "Script", "Layout", "ValueList", "Table", "Relationship",
    "CustomFunction", "CustomMenu", "CustomMenuSet", "Account", "PrivilegeSet",
    "ExtendedPrivilege", "FileReference", "Theme", "Options", "AuthFileCatalog",
    "BaseDirectoryCatalog",
}


def iter_defs(xml_path: str):
    """DDR を流し読みし、(種別, 要素, グループ階層) を返す。

    呼び出し側は受け取った要素を使い終えたら el.clear() してよい（解放される）。
    ヘッダ情報は ('#header', 要素, ()) として先に 2 回 yield する
    （FMPReport と File の属性）。
    """
    stack: list[str] = []
    groups: list[str] = []
    for event, el in ET.iterparse(xml_path, events=("start", "end")):
        if event == "start":
            stack.append(el.tag)
            if el.tag == "Group":
                groups.append(el.get("name") or "")
            elif el.tag in ("FMPReport", "File") and len(stack) <= 2:
                yield "#header", el, ()
            continue
        # end
        tag = stack.pop() if stack else el.tag
        if tag == "Group" and groups:
            groups.pop()
        if el.tag not in DEF_TAGS:
            continue
        path = "/".join(stack + [el.tag])
        for kind, rx in DEF_RE.items():
            if rx.match(path):
                yield kind, el, tuple(groups)
                break


def detect_type(xml_path: str) -> dict:
    """DDR の種別（Summary / Report / SaveAsXML）とヘッダ情報を判定する。"""
    info = {"root": None, "type": None, "version": None, "creationDate": None,
            "creationTime": None, "files": []}
    if not os.path.isfile(xml_path):
        raise SystemExit(f"ファイルが見つかりません: {xml_path}")
    try:
        for event, el in ET.iterparse(xml_path, events=("start", "end")):
            if event == "start":
                if info["root"] is None:
                    info["root"] = el.tag
                    info.update({k: el.get(k) for k in
                                 ("type", "version", "creationDate", "creationTime")
                                 if el.get(k)})
                continue
            if el.tag == "File":
                info["files"].append({
                    "name": el.get("name"), "link": el.get("link"),
                    "path": el.get("path"),
                    "counts": {c.tag: c.get("count") for c in el
                               if c.get("count") is not None},
                })
                el.clear()
                if info.get("type") != "Summary":
                    break
            elif el.tag in ("BaseTableCatalog", "FMSaveAsXML"):
                break
    except ET.ParseError as e:
        raise SystemExit(f"XML の解析に失敗しました: {xml_path}\n  {e}")
    return info


def resolve_link(base_dir: str, link: str) -> str | None:
    """概要ファイルの <File link="..."> を実ファイルに解決する。

    macOS の濁点表記ゆれ（NFC / NFD）や "./" 付きのリンクも吸収する。
    """
    if not link:
        return None
    cand = link.lstrip("./")
    direct = os.path.join(base_dir, cand)
    if os.path.exists(direct):
        return direct
    target = unicodedata.normalize("NFC", os.path.basename(cand))
    for entry in os.listdir(base_dir):
        if unicodedata.normalize("NFC", entry) == target:
            return os.path.join(base_dir, entry)
    return None


def input_files(path: str) -> tuple[dict, list[str]]:
    """入力から「処理すべき DDR 本体 XML のリスト」を作る。"""
    info = detect_type(path)
    if info["root"] == "FMSaveAsXML":
        raise SystemExit(
            "この XML は SaveAsXML 形式です。split は DDR（FMPReport）専用です。\n"
            "構造の調査には nodes / extract サブコマンドを使ってください。")
    if info["root"] != "FMPReport":
        raise SystemExit(f"FileMaker の DDR ではないようです（ルート要素: {info['root']}）")
    if info.get("type") == "Summary":
        base = os.path.dirname(os.path.abspath(path))
        targets, missing = [], []
        for f in info["files"]:
            hit = resolve_link(base, f.get("link") or "")
            (targets if hit else missing).append(hit or f.get("name"))
        if missing:
            eprint(f"警告: 概要ファイルからリンクを解決できませんでした: {', '.join(map(str, missing))}")
        if not targets:
            raise SystemExit("概要ファイルから本体 XML を 1 つも解決できませんでした。")
        return info, targets
    return info, [path]


# ---------------------------------------------------------------------------
# 3. 参照の収集
# ---------------------------------------------------------------------------

REF_KINDS = ("fields", "map_fields", "scripts", "layouts", "tos", "valuelists",
             "files", "menusets", "themes", "custommenus")


def new_refs() -> dict[str, set]:
    return {k: set() for k in REF_KINDS}


def collect_refs(el, into: dict[str, set] | None = None,
                 skip: tuple[str, ...] = ()) -> dict[str, set]:
    """要素配下に現れる「他オブジェクトへの参照」を集める。

    DDR は参照を必ず <Field table name> / <Script name> / <Layout name> /
    <Table name>（＝TO）/ <ValueList name> / <FileReference name> という
    共通の形で持つので、言語やステップ種別に依存せず横断的に拾える。
    """
    refs = into if into is not None else new_refs()
    for node in el.iter():
        if node is el:
            continue  # 自分自身は参照ではない
        tag = node.tag
        if tag in skip:
            continue
        name = node.get("name")
        if not name:
            continue
        if tag == "Field":
            # table 属性のないフィールドはインポート/エクスポートのマッピング先。
            # TO が特定できないので別枠にして、相互参照のキーを汚さない。
            if node.get("table") or node.get("baseTable"):
                refs["fields"].add(field_key(node))
            else:
                refs["map_fields"].add(name)
        elif tag == "Script":
            refs["scripts"].add(name)
        elif tag == "Layout":
            refs["layouts"].add(name)
        elif tag == "Table":
            refs["tos"].add(name)
        elif tag == "ValueList":
            refs["valuelists"].add(name)
        elif tag == "FileReference":
            refs["files"].add(name)
        elif tag == "CustomMenuSet":
            refs["menusets"].add(name)
        elif tag == "CustomMenu":
            refs["custommenus"].add(name)
        elif tag == "Theme":
            refs["themes"].add(name)
    return refs


def refs_to_json(refs: dict[str, set]) -> dict[str, list]:
    return {k: sorted(v) for k, v in refs.items() if v}


# ---------------------------------------------------------------------------
# 4. 各カタログのレンダリング
# ---------------------------------------------------------------------------

# 制御構造ステップの ID（言語非依存）。(行を書く前の増減, 書いた後の増減)
CTRL_STEPS = {
    "68": (0, 1),    # If
    "125": (-1, 1),  # Else If
    "69": (-1, 1),   # Else
    "70": (-1, 0),   # End If
    "71": (0, 1),    # Loop
    "73": (-1, 0),   # End Loop
}


def render_script_body(script_el) -> tuple[str, int, int]:
    """StepList を FileMaker のスクリプトワークスペース風テキストに整形する。

    StepText に表示用のステップ文字列がそのまま入っているので、
    それを制御構造 ID から算出した字下げとともに並べる。
    無効化ステップの StepText は DDR 側で既に "//  " が付いているため、
    こちらで重ねて付けない（件数だけ数える）。
    """
    lines: list[str] = []
    indent, total, disabled = 0, 0, 0
    for step in script_el.iterfind("StepList/Step"):
        total += 1
        sid = step.get("id") or ""
        off = not is_true(step.get("enable"))
        pre, post = CTRL_STEPS.get(sid, (0, 0))
        if pre < 0 and indent == 0:
            # 対応する If / Loop が無い孤立した Else / End If。
            # 構造が破れているので字下げを動かさない。
            pre, post = 0, 0
        indent = max(0, indent + pre)
        body = text_of(step.find("StepText")).rstrip("\n")
        if off:
            disabled += 1
        if not body:
            # 空のコメントステップは空行として残す（それ以外はステップ名で代替）
            body = "" if sid == "89" else (step.get("name") or "")
        head, *rest = body.split("\n")
        pad = "  " * (indent + 1)
        lines.append(f"{pad}{head}".rstrip() if head else "")
        lines.extend(rest)
        indent = max(0, indent + post)
    return "\n".join(lines), total, disabled


def parse_script(el, groups) -> dict:
    body, total, disabled = render_script_body(el)
    refs = collect_refs(el)
    return {
        "name": el.get("name") or "（無名）",
        "id": el.get("id"),
        "group": " / ".join(g for g in groups if g),
        "includeInMenu": el.get("includeInMenu"),
        "runFullAccess": el.get("runFullAccess"),
        "steps": total,
        "disabled": disabled,
        "body": body,
        "refs": refs,
    }


def parse_field(f) -> dict:
    """BaseTable のフィールド 1 件を辞書化する。"""
    st = f.find("Storage")
    flags = []
    if st is not None:
        if is_true(st.get("global")):
            flags.append("グローバル")
        rep = int(st.get("maxRepetition") or 1)
        if rep > 1:
            flags.append(f"繰り返し{rep}")
        if st.get("storeCalculationResults") == "False":
            flags.append("非保存")
        idx = st.get("index")
        if idx and idx != "None":
            flags.append(f"索引:{idx}")
        elif is_true(st.get("autoIndex")):
            flags.append("索引:必要時")
    refs = new_refs()
    details: list[str] = []

    calc = f.find("Calculation")
    if calc is not None and text_of(calc).strip():
        details.append(f"計算式 (コンテキスト: {calc.get('table') or '-'}):\n{text_of(calc).strip()}")
    disp = f.find("DisplayCalculation")
    if disp is not None:
        collect_refs(disp, refs)

    ae = f.find("AutoEnter")
    if ae is not None:
        if is_true(ae.get("calculation")) or ae.find("Calculation") is not None:
            c = ae.find("Calculation")
            if c is not None and text_of(c).strip():
                mode = "常に評価" if is_true(ae.get("alwaysEvaluate")) else "空欄時のみ評価"
                details.append(
                    f"自動入力 計算値 ({mode}, コンテキスト: {c.get('table') or '-'}):\n"
                    f"{text_of(c).strip()}")
            d = ae.find("DisplayCalculation")
            if d is not None:
                collect_refs(d, refs)
        lk = ae.find("Lookup")
        if lk is not None:
            src_tbl = lk.find("Table")
            src_fld = lk.find("Field")
            details.append(
                "ルックアップ: "
                f"{(src_tbl.get('name') if src_tbl is not None else '?')} 経由 → "
                f"{field_key(src_fld) if src_fld is not None else '?'}"
                f"（不一致時: {(lk.find('NoMatchCopyOption').get('value') if lk.find('NoMatchCopyOption') is not None else '-')}）")
            collect_refs(lk, refs)
        ser = ae.find("Serial")
        if ser is not None:
            details.append(
                f"シリアル番号: 次の値={ser.get('nextValue')} 増分={ser.get('increment')} "
                f"生成={ser.get('generate')}")
        const = text_of(ae.find("ConstantData")).strip()
        if const:
            details.append(f"自動入力 データ: {const}")
        if ae.get("value"):
            details.append(f"自動入力 作成/修正情報: {ae.get('value')}")
        if ae.get("furigana") == "True":
            details.append("自動入力 ふりがな: あり")
        if ae.get("allowEditing") == "False":
            details.append("入力値の変更を許さない")

    va = f.find("Validation")
    if va is not None:
        v = []
        for tag, label in (("NotEmpty", "空欄不可"),
                           ("Unique", "ユニーク"), ("Existing", "存在する値"),
                           ("StrictDataType", "データタイプ"), ("StrictValidation", "常に検証")):
            sub = va.find(tag)
            if sub is not None and is_true(sub.get("value")):
                v.append(label)
        vl = va.find("ValueList")
        if vl is not None and vl.get("name"):
            v.append(f"値一覧:{vl.get('name')}")
            refs["valuelists"].add(vl.get("name"))
        if va.get("maxLength") and va.get("maxLength") != "False":
            v.append(f"最大文字数:{va.get('maxLength')}")
        vc = va.find("Calculation")
        if vc is not None and text_of(vc).strip():
            v.append(f"計算による制限: {one_line(text_of(vc))}")
        msg = text_of(va.find("ErrorMessage")).strip()
        if msg:
            v.append(f"メッセージ:{one_line(msg)}")
        if v:
            details.append(f"入力値の制限 ({va.get('type') or '-'}): " + ", ".join(v))

    si = f.find("SummaryInfo")
    if si is not None:
        tgt = si.find("SummaryField/Field")
        details.append(
            f"集計: {si.get('operation')} of "
            f"{(tgt.get('name') if tgt is not None else '?')}"
            f"（グループごとに再開={si.get('restartForEachSortedGroup')}）")

    comment = text_of(f.find("Comment")).strip()
    return {
        "id": f.get("id"),
        "name": f.get("name") or "",
        "dataType": f.get("dataType"),
        "fieldType": f.get("fieldType"),
        "flags": flags,
        "comment": comment,
        "details": details,
        "refs": refs,
    }


def parse_basetable(el) -> dict:
    fields = [parse_field(f) for f in el.iterfind("FieldCatalog/Field")]
    refs = new_refs()
    for f in fields:
        for k in REF_KINDS:
            refs[k] |= f["refs"][k]
    return {
        "name": el.get("name") or "（無名）",
        "id": el.get("id"),
        "records": el.get("records"),
        "comment": (el.get("comment") or "").strip(),
        "fields": fields,
        "refs": refs,
    }


# --- レイアウト -------------------------------------------------------------

OBJ_CHILD = {
    "Field": "FieldObj", "Text": "TextObj", "Portal": "PortalObj",
    "Button": "ButtonObj", "GroupButton": "GroupButtonObj",
    "PopoverButton": "PopoverButtonObj", "Popover": "PopoverObj",
    "TabControl": "TabControlObj", "TabPanel": "TabPanelObj",
    "SlideControl": "SlideControlObj", "SlidePanel": "SlidePanelObj",
    "WebViewer": "WebViewerObj", "Chart": "ExternalObj", "Group": "GroupObj",
    "Rectangle": "SimpleObj", "RoundRect": "SimpleObj", "Oval": "SimpleObj",
    "Line": "SimpleObj", "Image": "ImageObj",
}


def obj_payload(obj):
    """<Object type="..."> の中身（型別の子要素）を返す。"""
    want = OBJ_CHILD.get(obj.get("type") or "")
    if want is not None:
        found = obj.find(want)
        if found is not None:
            return found
    for child in obj:
        if child.tag.endswith("Obj"):
            return child
    return None


def describe_object(obj, payload) -> str:
    """オブジェクトを 1 行で説明する。"""
    t = obj.get("type") or "?"
    if payload is None:
        return ""
    if t == "Field":
        name = text_of(payload.find("Name")).strip()
        ddr = payload.find("DDRInfo/Field")
        if not name and ddr is not None:
            name = field_key(ddr)
        return name
    if t == "Text":
        parts = [text_of(d) for d in payload.iterfind("CharacterStyleVector/Style/Data")]
        body = one_line("".join(parts), 120)
        merge = [field_key(f) for f in payload.iterfind("FieldList/Field")]
        out = f'"{body}"' if body else ""
        if merge:
            out += f"  マージ: {', '.join(merge)}"
        return out
    if t == "Portal":
        rows = payload.get("numOfRows")
        alias = text_of(payload.find("TableAliasKey")).strip()
        return f"{alias}  行数={rows}"
    if t in ("Button", "GroupButton", "PopoverButton"):
        step = payload.find("Step")
        act = one_line(text_of(step.find("StepText")), 120) if step is not None else ""
        img = payload.find("Image")
        label = f" アイコン:{img.get('name')}" if img is not None and img.get("name") else ""
        return (f"→ {act}" if act else "（動作なし）") + label
    if t == "WebViewer":
        calc = payload.find(".//Calculation")
        return one_line(text_of(calc), 120)
    if t == "Chart":
        title = payload.find("Title/Text/Calculation")
        return f"{payload.get('typeID') or ''} {one_line(text_of(title), 80)}".strip()
    if t in ("TabPanel", "SlidePanel"):
        return one_line("".join(text_of(d) for d in
                               payload.iterfind(".//CharacterStyleVector/Style/Data")), 60)
    if t == "Image":
        img = payload.find("Image")
        return img.get("name") if img is not None and img.get("name") else ""
    return ""


def render_objects(container, depth: int, out: list[str]):
    """オブジェクトを入れ子のまま字下げして書き出す。"""
    for obj in container.iterfind("Object"):
        payload = obj_payload(obj)
        b = obj.find("Bounds")
        pos = ""
        if b is not None:
            pos = (f"  ({num(b.get('top'))},{num(b.get('left'))},"
                   f"{num(b.get('bottom'))},{num(b.get('right'))})")
        desc = describe_object(obj, payload)
        pad = "  " * depth
        out.append(f"{pad}[{obj.get('type') or '?'}] {desc}{pos}".rstrip())
        name = obj.get("name")
        if name:
            out.append(f"{pad}  オブジェクト名: {name}")
        hide = obj.find("HideCondition/Calculation")
        if hide is not None and text_of(hide).strip():
            out.append(f"{pad}  非表示条件: {one_line(text_of(hide))}")
        cf = obj.findall("ConditionalFormatting/Item")
        for item in cf:
            cond = item.find("Condition/Calculation")
            if cond is not None and text_of(cond).strip():
                out.append(f"{pad}  条件付き書式: {one_line(text_of(cond))}")
        for trig in iter_triggers(obj):
            scr = trig.find("Script")
            sn = scr.get("name") if scr is not None and scr.get("name") else "-"
            out.append(f"{pad}  トリガ {trig.get('event')} → 「{sn}」")
        if payload is not None and payload.find("Object") is not None:
            render_objects(payload, depth + 1, out)
        # ポータル・タブ等のソート順
        for srt in obj.iterfind("*/SortList/Sort"):
            pf = srt.find("PrimaryField/Field")
            if pf is not None:
                out.append(f"{pad}  ソート: {field_key(pf)} ({srt.get('type')})")


def iter_triggers(el):
    """スクリプトトリガを返す。DDR では Layout / Object の下に
    ScriptTriggers/Trigger の形で入っている（直下 Trigger の形も一応拾う）。"""
    yield from el.iterfind("ScriptTriggers/Trigger")
    yield from el.iterfind("Trigger")


def parse_layout(el, groups) -> dict:
    tbl = el.find("Table")
    theme = el.find("Theme")
    menuset = el.find("CustomMenuSet")
    body: list[str] = []
    render_objects(el, 0, body)
    triggers = []
    for trig in iter_triggers(el):
        scr = trig.find("Script")
        triggers.append({"event": trig.get("event"),
                         "script": scr.get("name") if scr is not None else None,
                         "text": one_line(text_of(trig.find("TriggerText")))})
    refs = collect_refs(el)
    portals = sorted({text_of(p.find("TableAliasKey")).strip()
                      for p in el.iter("PortalObj")
                      if text_of(p.find("TableAliasKey")).strip()})
    return {
        "name": el.get("name") or "（無名）",
        "id": el.get("id"),
        "group": " / ".join(g for g in groups if g),
        "to": tbl.get("name") if tbl is not None else None,
        "theme": theme.get("name") if theme is not None else None,
        "menuset": (menuset.get("name") or None) if menuset is not None else None,
        "width": el.get("width"),
        "quickFind": el.get("quickFind"),
        "includeInMenu": el.get("includeInMenu"),
        "objects": len(list(el.iter("Object"))),
        "portals": portals,
        "triggers": triggers,
        "body": "\n".join(body),
        "refs": refs,
    }


# --- 値一覧・リレーション・メニュー ----------------------------------------

def parse_valuelist(el) -> dict:
    src = el.find("Source")
    kind = src.get("value") if src is not None else "?"
    lines = [f"種類: {kind}"]
    if kind == "Custom":
        vals = text_of(el.find("CustomValues/Text")).rstrip("\n")
        lines.append("値:")
        lines.extend("  " + v for v in vals.split("\n"))
    else:
        for tag, label in (("PrimaryField", "1 番目のフィールド"),
                           ("SecondaryField", "2 番目のフィールド")):
            sub = el.find(tag)
            if sub is None:
                continue
            f = sub.find("Field")
            lines.append(f"{label}: {field_key(f) if f is not None else '?'}"
                         f"（表示={sub.get('show')}, ソート={sub.get('sort')}）")
        rel = el.find("ShowRelated")
        if rel is not None:
            t = rel.find("Table")
            lines.append("関連レコードのみ: " + (f"はい（{t.get('name')} から）"
                                                if is_true(rel.get("value")) and t is not None
                                                else "いいえ"))
    return {"name": el.get("name") or "（無名）", "id": el.get("id"),
            "kind": kind, "body": "\n".join(lines), "refs": collect_refs(el)}


def parse_relationship(el) -> dict:
    left, right = el.find("LeftTable"), el.find("RightTable")
    preds = []
    for p in el.iterfind("JoinPredicateList/JoinPredicate"):
        lf, rf = p.find("LeftField/Field"), p.find("RightField/Field")
        preds.append({"type": p.get("type"),
                      "left": field_key(lf) if lf is not None else "?",
                      "right": field_key(rf) if rf is not None else "?"})

    def side(t):
        if t is None:
            return {}
        srt = t.find("SortList")
        return {"name": t.get("name"),
                "cascadeCreate": is_true(t.get("cascadeCreate")),
                "cascadeDelete": is_true(t.get("cascadeDelete")),
                "sorted": srt is not None and is_true(srt.get("value"))}

    return {"id": el.get("id"), "left": side(left), "right": side(right),
            "predicates": preds, "refs": collect_refs(el)}


def parse_custommenu(el) -> dict:
    lines = []
    base = el.find("BaseMenu")
    if base is not None:
        lines.append(f"ベースメニュー: {base.get('name')}")
    opt = el.find("Options")
    if opt is not None:
        modes = [m for m, k in (("ブラウズ", "browseMode"), ("検索", "findMode"),
                                ("プレビュー", "previewMode")) if is_true(opt.get(k))]
        plats = [m for m, k in (("mac", "macPlatform"), ("win", "winPlatform"))
                 if is_true(opt.get(k))]
        lines.append(f"有効モード: {', '.join(modes) or '-'} / プラットフォーム: {', '.join(plats) or '-'}")
    inst = el.find("Conditions/Install/Calculation")
    if inst is not None and text_of(inst).strip() not in ("", "1"):
        lines.append(f"インストール条件: {one_line(text_of(inst))}")
    items = list(el.iterfind("MenuItemList/MenuItem"))
    if items:
        lines.append(f"\n項目 ({len(items)}):")
    for it in items:
        # 項目名は Name/Calculation（計算式なので前後の " を外す）
        label = one_line(text_of(it.find("Name/Calculation")).strip(), 80)
        if label.startswith('"') and label.endswith('"') and len(label) > 1:
            label = label[1:-1]
        cmd = it.find("Command")
        cmd_name = cmd.get("name") if cmd is not None else None
        step = it.find("Step")
        act = one_line(text_of(step.find("StepText")), 100) if step is not None else ""
        if is_true(it.get("isSeparatorItem")):
            lines.append("  - ―――（区切り）")
            continue
        parts = [p for p in (label or cmd_name or "（名称なし）",
                             cmd_name if label else None, act) if p]
        sc = it.find("Shortcut")
        if sc is not None and sc.get("key"):
            parts.append(f"ショートカット key={sc.get('key')} mod={sc.get('modifier')}")
        inst = it.find("Conditions/Install/Calculation")
        if inst is not None and text_of(inst).strip() not in ("", "1"):
            parts.append(f"インストール条件: {one_line(text_of(inst), 60)}")
        lines.append("  - " + " | ".join(parts))
    return {"name": el.get("name") or "（無名）", "id": el.get("id"),
            "body": "\n".join(lines), "refs": collect_refs(el)}


def parse_menuset(el) -> dict:
    menus = [m.get("name") for m in el.iterfind("CustomMenuList/CustomMenu")]
    return {"name": el.get("name") or "（無名）", "id": el.get("id"),
            "comment": text_of(el.find("Comment")).strip(), "menus": menus,
            "refs": collect_refs(el)}


def parse_customfunction(el) -> dict:
    params = [p.get("name") or text_of(p) for p in el.iterfind("*/Parameter")]
    calc = el.find("Calculation")
    return {"name": el.get("name") or "（無名）", "id": el.get("id"),
            "params": [p for p in params if p],
            "availability": el.get("availability") or el.get("visible"),
            "body": text_of(calc).strip(), "refs": collect_refs(el)}


# ---------------------------------------------------------------------------
# 5. 1 ファイル分の解析
# ---------------------------------------------------------------------------

def parse_solution(xml_path: str, opts) -> dict:
    sol = {
        "source_xml": os.path.abspath(xml_path),
        "file_name": None, "file_path": None,
        "ddr": {}, "options": {},
        "base_tables": [], "scripts": [], "layouts": [], "valuelists": [],
        "tos": [], "relationships": [], "custom_functions": [],
        "custom_menus": [], "menu_sets": [], "accounts": [],
        "privilege_sets": [], "ext_privileges": [], "file_refs": [],
        "themes": [], "auth_files": [], "base_dirs": [],
    }
    for kind, el, groups in iter_defs(xml_path):
        if kind == "#header":
            if el.tag == "FMPReport":
                sol["ddr"] = {k: el.get(k) for k in
                              ("type", "version", "creationDate", "creationTime")}
            else:
                sol["file_name"] = el.get("name")
                sol["file_path"] = el.get("path")
            continue

        if kind == "basetable":
            sol["base_tables"].append(parse_basetable(el))
        elif kind == "script":
            sol["scripts"].append(parse_script(el, groups))
        elif kind == "layout":
            sol["layouts"].append(parse_layout(el, groups))
        elif kind == "valuelist":
            sol["valuelists"].append(parse_valuelist(el))
        elif kind == "to":
            fr = el.find("FileReference")
            sol["tos"].append({
                "name": el.get("name"), "id": el.get("id"),
                "baseTable": el.get("baseTable"), "baseTableId": el.get("baseTableId"),
                "file": fr.get("name") if fr is not None else None,
                "color": el.get("color")})
        elif kind == "relationship":
            sol["relationships"].append(parse_relationship(el))
        elif kind == "customfunction":
            sol["custom_functions"].append(parse_customfunction(el))
        elif kind == "custommenu":
            sol["custom_menus"].append(parse_custommenu(el))
        elif kind == "menuset":
            sol["menu_sets"].append(parse_menuset(el))
        elif kind == "account":
            desc = text_of(el.find("Description")).strip()
            sol["accounts"].append({
                "name": el.get("name"), "id": el.get("id"),
                "privilegeSet": el.get("privilegeSet"), "status": el.get("status"),
                "managedBy": el.get("managedBy"),
                "emptyPassword": el.get("emptyPassword"),
                "description": desc if opts.include_account_notes else
                               ("（説明あり: --include-account-notes で出力）" if desc else "")})
        elif kind == "privilegeset":
            rows = {}
            for tag in ("Records", "Layouts", "ValueLists", "Scripts"):
                sub = el.find(tag)
                if sub is not None:
                    rows[tag] = sub.get("value") + (
                        " (新規可)" if is_true(sub.get("allowCreation")) else "")
            tables = []
            for bt in el.iterfind(".//BaseTable"):
                acc = {t.tag: (t.get("value") or "") for t in bt}
                tables.append({"name": bt.get("name"), **acc})
            sol["privilege_sets"].append({
                "name": el.get("name"), "id": el.get("id"),
                "comment": el.get("comment") or "", "menu": el.get("menu"),
                "printing": el.get("printing"), "exporting": el.get("exporting"),
                "manageAccounts": el.get("manageAccounts"),
                "areas": rows, "tables": tables})
        elif kind == "extprivilege":
            sol["ext_privileges"].append({
                "name": el.get("name"), "id": el.get("id"),
                "comment": el.get("comment") or el.get("description") or "",
                "sets": [p.get("name") for p in el.iterfind(".//PrivilegeSet")]})
        elif kind == "fileref":
            sol["file_refs"].append({
                "name": el.get("name"), "id": el.get("id"),
                "pathList": el.get("pathList"), "link": el.get("link")})
        elif kind == "theme":
            sol["themes"].append({"name": el.get("name"), "id": el.get("id")})
        elif kind == "options":
            oo = el.find("OnOpen")
            opt = {"encryption": (el.find("Encryption").get("type")
                                  if el.find("Encryption") is not None else None)}
            if oo is not None:
                acct = oo.find("Account")
                lay = oo.find("Layout")
                minv = oo.find("MinimumAllowedVersion")
                opt.update({
                    "onOpenAccount": acct.get("name") if acct is not None else None,
                    "onOpenLayout": lay.get("name") if lay is not None else None,
                    "minVersion": minv.get("name") if minv is not None else None})
            trg = {}
            wt = el.find("WindowTriggers")
            if wt is not None:
                for t in wt:
                    scr = t.find("Script")
                    trg[t.tag] = scr.get("name") if scr is not None else None
            opt["triggers"] = {k: v for k, v in trg.items() if v}
            for tag, key in (("DefaultCustomMenuSet", "defaultMenuSet"),
                             ("DefaultTheme", "defaultTheme")):
                sub = el.find(tag)
                if sub is not None and len(sub):
                    opt[key] = sub[0].get("name")
            sol["options"] = opt
        elif kind == "authfile":
            sol["auth_files"] = [{
                "id": o.get("id"), "date": o.get("date"),
                "user": o.get("user"), "files": o.get("filenames")}
                for o in el.iterfind(".//OutboundAuthorization")]
        elif kind == "basedir":
            sol["base_dirs"] = [{"id": d.get("id"), "path": text_of(d).strip()}
                                for d in el.iterfind("BaseDirectoryList/BaseDirectory")]
        el.clear()
    return sol


# ---------------------------------------------------------------------------
# 6. 相互参照の構築
# ---------------------------------------------------------------------------

def build_xref(sol: dict) -> dict:
    """解析結果から「どこで使われているか」の逆引きを作る。"""
    def add(bucket: dict, key, where: str, value: str):
        if not key:
            return
        bucket.setdefault(key, {}).setdefault(where, set()).add(value)

    fields: dict = {}
    scripts: dict = {}
    layouts: dict = {}
    tos: dict = {}
    valuelists: dict = {}
    base_tables: dict = {}
    files: dict = {}

    for s in sol["scripts"]:
        scripts.setdefault(s["name"], {})
        for f in s["refs"]["fields"]:
            add(fields, f, "scripts", s["name"])
        for other in s["refs"]["scripts"]:
            add(scripts, other, "called_by", s["name"])
            add(scripts, s["name"], "calls", other)
        for lay in s["refs"]["layouts"]:
            add(layouts, lay, "scripts", s["name"])
        for to in s["refs"]["tos"]:
            add(tos, to, "scripts", s["name"])
        for vl in s["refs"]["valuelists"]:
            add(valuelists, vl, "scripts", s["name"])
        for fl in s["refs"]["files"]:
            add(files, fl, "scripts", s["name"])

    for lay in sol["layouts"]:
        layouts.setdefault(lay["name"], {})
        for f in lay["refs"]["fields"]:
            add(fields, f, "layouts", lay["name"])
        for sc in lay["refs"]["scripts"]:
            add(scripts, sc, "layouts", lay["name"])
        for to in lay["refs"]["tos"]:
            add(tos, to, "layouts", lay["name"])
        for vl in lay["refs"]["valuelists"]:
            add(valuelists, vl, "layouts", lay["name"])
        if lay["to"]:
            add(tos, lay["to"], "layouts", lay["name"])

    for bt in sol["base_tables"]:
        base_tables.setdefault(bt["name"], {})
        for f in bt["fields"]:
            key = f"{bt['name']}::{f['name']}"
            fields.setdefault(key, {})
            for ref in f["refs"]["fields"]:
                add(fields, ref, "field_calcs", key)
            for vl in f["refs"]["valuelists"]:
                add(valuelists, vl, "validations", key)

    for to in sol["tos"]:
        tos.setdefault(to["name"], {})
        if to["baseTable"]:
            add(base_tables, to["baseTable"], "occurrences", to["name"])

    for rel in sol["relationships"]:
        ln, rn = rel["left"].get("name"), rel["right"].get("name")
        for a, b in ((ln, rn), (rn, ln)):
            add(tos, a, "relationships", f"{ln} — {rn}")
        for p in rel["predicates"]:
            add(fields, p["left"], "relationships", f"{ln} — {rn}")
            add(fields, p["right"], "relationships", f"{ln} — {rn}")

    for vl in sol["valuelists"]:
        valuelists.setdefault(vl["name"], {})
        for f in vl["refs"]["fields"]:
            add(fields, f, "valuelists", vl["name"])
        for to in vl["refs"]["tos"]:
            add(tos, to, "valuelists", vl["name"])

    for cm in sol["custom_menus"]:
        for sc in cm["refs"]["scripts"]:
            add(scripts, sc, "custom_menus", cm["name"])
        for lay in cm["refs"]["layouts"]:
            add(layouts, lay, "custom_menus", cm["name"])
        for f in cm["refs"]["fields"]:
            add(fields, f, "custom_menus", cm["name"])

    for cf in sol["custom_functions"]:
        for f in cf["refs"]["fields"]:
            add(fields, f, "custom_functions", cf["name"])

    for name, sc in (sol.get("options", {}).get("triggers") or {}).items():
        add(scripts, sc, "file_triggers", name)

    def freeze(d):
        return {k: {w: sorted(v) for w, v in sorted(where.items())}
                for k, where in sorted(d.items())}

    return {"fields": freeze(fields), "scripts": freeze(scripts),
            "layouts": freeze(layouts), "table_occurrences": freeze(tos),
            "base_tables": freeze(base_tables), "valuelists": freeze(valuelists),
            "external_files": freeze(files)}


# ---------------------------------------------------------------------------
# 7. 出力（Markdown）
# ---------------------------------------------------------------------------

class Writer:
    """生成したファイルを記録しながら書き出す（--clean 用）。"""

    def __init__(self, root: str):
        self.root = root
        self.written: list[str] = []

    def write(self, rel_path: str, lines: list[str] | str):
        body = lines if isinstance(lines, str) else "\n".join(lines)
        full = os.path.join(self.root, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(body.rstrip("\n") + "\n")
        self.written.append(rel_path)
        return rel_path


def ref_lines(refs: dict[str, set], xref_extra: list[str] | None = None) -> list[str]:
    labels = [("scripts", "呼び出すスクリプト"), ("layouts", "参照レイアウト"),
              ("tos", "参照 TO"), ("fields", "参照フィールド"),
              ("map_fields", "参照フィールド（TO 不明: インポート/エクスポート対応）"),
              ("valuelists", "参照値一覧"), ("files", "参照外部ファイル"),
              ("custommenus", "参照カスタムメニュー")]
    out = []
    for key, label in labels:
        vals = sorted(refs.get(key) or ())
        if not vals:
            continue
        shown = ", ".join(vals[:40])
        more = f" …他 {len(vals) - 40} 件" if len(vals) > 40 else ""
        out.append(f"- {label} ({len(vals)}): {shown}{more}")
    out.extend(xref_extra or [])
    return out


def write_solution(sol: dict, xref: dict, w: Writer, base: str, opts) -> dict:
    """1 ソリューション分の Markdown を書き出し、index 用のメタを返す。"""
    idx = {"file_name": sol["file_name"], "source_xml": sol["source_xml"],
           "ddr": sol["ddr"], "counts": {}, "scripts": [], "base_tables": [],
           "layouts": [], "valuelists": [], "table_occurrences": [],
           "custom_menus": [], "menu_sets": [], "custom_functions": []}

    # --- スクリプト
    used: set[str] = set()
    for s in sol["scripts"]:
        fname = safe_filename(s["name"], used)
        rel = f"{base}/Scripts/{fname}"
        callers = sorted((xref["scripts"].get(s["name"], {}).get("called_by") or []))
        from_layouts = sorted((xref["scripts"].get(s["name"], {}).get("layouts") or []))
        from_menus = sorted((xref["scripts"].get(s["name"], {}).get("custom_menus") or []))
        from_file = sorted((xref["scripts"].get(s["name"], {}).get("file_triggers") or []))
        extra = []
        if callers:
            extra.append(f"- 呼び出し元スクリプト ({len(callers)}): {', '.join(callers[:40])}")
        if from_layouts:
            extra.append(f"- 呼び出し元レイアウト（ボタン/トリガ） ({len(from_layouts)}): "
                         f"{', '.join(from_layouts[:40])}")
        if from_menus:
            extra.append(f"- 呼び出し元カスタムメニュー: {', '.join(from_menus)}")
        if from_file:
            extra.append(f"- ファイルトリガ: {', '.join(from_file)}")
        if not (callers or from_layouts or from_menus or from_file):
            extra.append("- 呼び出し元: 検出なし（メニュー実行・動的呼び出しの可能性あり）")
        lines = [f"# Script: {s['name']}", ""]
        meta = [f"- ファイル: {sol['file_name']}", f"- ID: {s['id']}"]
        if s["group"]:
            meta.append(f"- スクリプトグループ: {s['group']}")
        meta.append(f"- ステップ数: {s['steps']}（無効: {s['disabled']}）")
        meta.append(f"- メニューに表示: {s['includeInMenu']} / 完全アクセス権で実行: {s['runFullAccess']}")
        lines += meta + ref_lines(s["refs"], extra) + ["", "## ステップ", ""]
        lines += fence(s["body"] or "（ステップなし）", "filemaker")
        w.write(rel, lines)
        idx["scripts"].append({"name": s["name"], "id": s["id"], "group": s["group"],
                               "path": rel, "steps": s["steps"],
                               "disabled": s["disabled"],
                               "refs": refs_to_json(s["refs"])})

    # --- テーブル定義
    used = set()
    for bt in sol["base_tables"]:
        fname = safe_filename(bt["name"], used)
        rel = f"{base}/BaseTables/{fname}"
        occ = sorted(xref["base_tables"].get(bt["name"], {}).get("occurrences") or [])
        lays = sorted({l["name"] for l in sol["layouts"]
                       if l["to"] and l["to"] in occ})
        kinds = {}
        for f in bt["fields"]:
            kinds[f["fieldType"]] = kinds.get(f["fieldType"], 0) + 1
        lines = [f"# BaseTable: {bt['name']}", "",
                 f"- ファイル: {sol['file_name']}",
                 f"- ID: {bt['id']} / レコード数: {bt['records']}",
                 f"- フィールド数: {len(bt['fields'])}"
                 f"（{', '.join(f'{k}:{v}' for k, v in sorted(kinds.items()))}）"]
        if bt["comment"]:
            lines.append(f"- コメント: {one_line(bt['comment'])}")
        if occ:
            lines.append(f"- このテーブルの TO ({len(occ)}): {', '.join(occ[:40])}")
        if lays:
            lines.append(f"- 関連レイアウト ({len(lays)}): {', '.join(lays[:40])}")
        lines += ["", "## フィールド一覧", ""]
        for f in bt["fields"]:
            flags = f"（{', '.join(f['flags'])}）" if f["flags"] else ""
            lines.append(f"[{f['id']}] {f['name']} — {f['dataType']} / {f['fieldType']}{flags}")
            if f["comment"]:
                lines.append(f"      # {one_line(f['comment'])}")
        detailed = [f for f in bt["fields"] if f["details"]]
        if detailed:
            lines += ["", "## 詳細（計算式・自動入力・入力値の制限）", ""]
            for f in detailed:
                lines.append(f"### [{f['id']}] {f['name']} ({f['dataType']} / {f['fieldType']})")
                for d in f["details"]:
                    if "\n" in d:
                        head, body = d.split("\n", 1)
                        lines.append(head)
                        lines += fence(body, "filemaker")
                    else:
                        lines.append(d)
                if f["refs"]["fields"]:
                    lines.append(f"参照フィールド: {', '.join(sorted(f['refs']['fields']))}")
                lines.append("")
        w.write(rel, lines)
        idx["base_tables"].append({
            "name": bt["name"], "id": bt["id"], "records": bt["records"],
            "path": rel, "fields": len(bt["fields"]),
            "field_names": [f["name"] for f in bt["fields"]],
            # 計算式・入力値の制限・命名の実態を後段（handover）で扱うための定義本体
            "field_defs": [{"id": f["id"], "name": f["name"],
                            "dataType": f["dataType"], "fieldType": f["fieldType"],
                            "flags": f["flags"], "comment": f["comment"],
                            "details": f["details"],
                            "refs": refs_to_json(f["refs"])}
                           for f in bt["fields"]]})

    # --- レイアウト
    used = set()
    for lay in sol["layouts"]:
        prefix = f"{lay['to']}_" if lay["to"] else ""
        fname = safe_filename(prefix + lay["name"], used)
        rel = f"{base}/Layouts/{fname}"
        bt_name = next((t["baseTable"] for t in sol["tos"] if t["name"] == lay["to"]), None)
        from_scripts = sorted(xref["layouts"].get(lay["name"], {}).get("scripts") or [])
        from_menus = sorted(xref["layouts"].get(lay["name"], {}).get("custom_menus") or [])
        lines = [f"# Layout: {lay['name']}", "",
                 f"- ファイル: {sol['file_name']}", f"- ID: {lay['id']}"]
        if lay["group"]:
            lines.append(f"- レイアウトグループ: {lay['group']}")
        lines.append(f"- TO: {lay['to']}"
                     + (f"（BaseTable: {bt_name}）" if bt_name else ""))
        lines.append(f"- テーマ: {lay['theme']} / メニューセット: {lay['menuset'] or '-'}")
        lines.append(f"- 幅: {lay['width']} / クイック検索: {lay['quickFind']} /"
                     f" メニューに表示: {lay['includeInMenu']}")
        lines.append(f"- オブジェクト数: {lay['objects']}")
        if lay["portals"]:
            lines.append(f"- ポータル ({len(lay['portals'])}): {', '.join(lay['portals'])}")
        for t in lay["triggers"]:
            lines.append(f"- レイアウトトリガ {t['event']} → 「{t['script'] or '-'}」")
        if from_scripts:
            lines.append(f"- このレイアウトを使うスクリプト ({len(from_scripts)}): "
                         f"{', '.join(from_scripts[:40])}")
        if from_menus:
            lines.append(f"- 参照元カスタムメニュー: {', '.join(from_menus)}")
        lines += ref_lines(lay["refs"])
        lines += ["", "## オブジェクト", ""]
        lines += fence(lay["body"] or "（オブジェクトなし）")
        w.write(rel, lines)
        idx["layouts"].append({"name": lay["name"], "id": lay["id"],
                               "to": lay["to"], "base_table": bt_name,
                               "group": lay["group"], "path": rel,
                               "objects": lay["objects"],
                               "portals": lay["portals"],
                               "triggers": [{"event": t["event"], "script": t["script"]}
                                            for t in lay["triggers"]],
                               "refs": refs_to_json(lay["refs"])})

    # --- リレーションシップ
    rel_path = f"{base}/Relationships.md"
    lines = [f"# リレーションシップ: {sol['file_name']}", "",
             f"- TO 数: {len(sol['tos'])} / リレーション数: {len(sol['relationships'])}",
             "- [作成] = 連鎖作成, [削除] = 連鎖削除", "", "## テーブルオカレンス（TO）", ""]
    for t in sorted(sol["tos"], key=lambda x: (x["name"] or "")):
        ext = f" ← 外部ファイル: {t['file']}" if t["file"] else ""
        lines.append(f"- {t['name']}  (BaseTable: {t['baseTable']}, ID: {t['id']}){ext}")
    lines += ["", "## リレーション", ""]
    for r in sol["relationships"]:
        ln, rn = r["left"].get("name"), r["right"].get("name")
        flags = []
        for side, label in ((r["left"], "左"), (r["right"], "右")):
            f = [n for n, k in (("作成", "cascadeCreate"), ("削除", "cascadeDelete"))
                 if side.get(k)]
            if f:
                flags.append(f"{label}:[{'/'.join(f)}]")
            if side.get("sorted"):
                flags.append(f"{label}:ソート指定")
        lines.append(f"### [{r['id']}] {ln} — {rn}" + (f"  {' '.join(flags)}" if flags else ""))
        for p in r["predicates"]:
            lines.append(f"- {p['left']}  *{p['type']}*  {p['right']}")
        lines.append("")
    w.write(rel_path, lines)
    idx["table_occurrences"] = [{"name": t["name"], "base_table": t["baseTable"],
                                 "file": t["file"], "id": t["id"]} for t in sol["tos"]]

    # --- 値一覧
    lines = [f"# 値一覧: {sol['file_name']}", "", f"- 件数: {len(sol['valuelists'])}", ""]
    for vl in sorted(sol["valuelists"], key=lambda x: x["name"]):
        where = xref["valuelists"].get(vl["name"], {})
        used_in = []
        for key, label in (("layouts", "レイアウト"), ("scripts", "スクリプト"),
                           ("validations", "入力値の制限")):
            v = where.get(key) or []
            if v:
                used_in.append(f"{label}: {', '.join(sorted(v)[:20])}")
        lines.append(f"## [{vl['id']}] {vl['name']}")
        lines.append(vl["body"])
        lines.append("使用箇所: " + ("; ".join(used_in) if used_in else "検出なし"))
        lines.append("")
    w.write(f"{base}/ValueLists.md", lines)
    idx["valuelists"] = [{"name": v["name"], "id": v["id"], "kind": v["kind"]}
                         for v in sol["valuelists"]]

    # --- カスタム関数
    lines = [f"# カスタム関数: {sol['file_name']}", "",
             f"- 件数: {len(sol['custom_functions'])}", ""]
    for cf in sol["custom_functions"]:
        lines.append(f"## {cf['name']} ( {' ; '.join(cf['params'])} )")
        if cf["availability"]:
            lines.append(f"- 利用可能: {cf['availability']}")
        lines += fence(cf["body"] or "（空）", "filemaker") + [""]
    if not sol["custom_functions"]:
        lines.append("（このファイルにカスタム関数はありません）")
    w.write(f"{base}/CustomFunctions.md", lines)
    idx["custom_functions"] = [{"name": c["name"], "params": c["params"]}
                               for c in sol["custom_functions"]]

    # --- カスタムメニュー
    lines = [f"# カスタムメニュー: {sol['file_name']}", "",
             f"- メニュー: {len(sol['custom_menus'])} / メニューセット: {len(sol['menu_sets'])}",
             "", "## メニューセット", ""]
    for ms in sol["menu_sets"]:
        lines.append(f"### [{ms['id']}] {ms['name']}")
        if ms["comment"]:
            lines.append(f"- コメント: {one_line(ms['comment'])}")
        lines.append(f"- 含まれるメニュー ({len(ms['menus'])}): {', '.join(ms['menus']) or '-'}")
        lines.append("")
    lines += ["## メニュー", ""]
    for cm in sol["custom_menus"]:
        lines.append(f"### [{cm['id']}] {cm['name']}")
        lines.append(cm["body"])
        lines.append("")
    w.write(f"{base}/CustomMenus.md", lines)
    idx["custom_menus"] = [{"name": c["name"], "id": c["id"]} for c in sol["custom_menus"]]
    idx["menu_sets"] = [{"name": m["name"], "id": m["id"], "menus": m["menus"]}
                        for m in sol["menu_sets"]]

    # --- セキュリティ
    lines = [f"# アカウントとアクセス権: {sol['file_name']}", "",
             "> このファイルには実在のアカウント名が含まれます。外部への共有には注意してください。",
             "", "## アカウント", ""]
    for a in sol["accounts"]:
        lines.append(f"- {a['name']}  権限セット: {a['privilegeSet']} / 状態: {a['status']}"
                     f" / 管理: {a['managedBy']} / 空パスワード: {a['emptyPassword']}")
        if a["description"]:
            lines.append(f"    説明: {one_line(a['description'])}")
    lines += ["", "## 権限セット", ""]
    for p in sol["privilege_sets"]:
        lines.append(f"### {p['name']}")
        if p["comment"]:
            lines.append(f"- 説明: {p['comment']}")
        lines.append(f"- メニュー: {p['menu']} / 印刷: {p['printing']} /"
                     f" エクスポート: {p['exporting']} / アカウント管理: {p['manageAccounts']}")
        for k, v in p["areas"].items():
            lines.append(f"- {k}: {v}")
        limited = [t for t in p["tables"]
                   if any(v not in ("", "Yes", "All") for k, v in t.items() if k != "name")]
        if limited:
            lines.append(f"- テーブル別アクセス（抜粋 {min(len(limited), 30)}/{len(limited)}）:")
            for t in limited[:30]:
                detail = ", ".join(f"{k}={v}" for k, v in t.items() if k != "name" and v)
                lines.append(f"    - {t['name']}: {detail}")
        lines.append("")
    lines += ["## 拡張アクセス権", ""]
    for e in sol["ext_privileges"]:
        lines.append(f"- {e['name']}"
                     + (f"（{e['comment']}）" if e["comment"] else "")
                     + (f" → {', '.join(e['sets'])}" if e["sets"] else ""))
    if sol["auth_files"]:
        lines += ["", "## ファイルアクセスの承認（Outbound）", ""]
        for o in sol["auth_files"]:
            lines.append(f"- {o['date']} / {o['user']} / {o['files']}")
    w.write(f"{base}/Security.md", lines)

    # --- ファイル情報
    o = sol["options"]
    lines = [f"# ファイル情報: {sol['file_name']}", "",
             f"- DDR: FileMaker {sol['ddr'].get('version')} /"
             f" 出力日時: {sol['ddr'].get('creationDate')} {sol['ddr'].get('creationTime')}",
             f"- 元ファイルパス: {sol['file_path']}",
             f"- 解析元 XML: {sol['source_xml']}",
             f"- fmddr: {VERSION} / 解析日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             "", "## ファイルオプション", "",
             f"- 起動アカウント: {o.get('onOpenAccount')} / 起動レイアウト: {o.get('onOpenLayout') or '-'}",
             f"- 最小許容バージョン: {o.get('minVersion')} / 暗号化: {o.get('encryption')}",
             f"- 既定メニューセット: {o.get('defaultMenuSet')} / 既定テーマ: {o.get('defaultTheme')}"]
    for k, v in (o.get("triggers") or {}).items():
        lines.append(f"- ファイルトリガ {k} → 「{v}」")
    lines += ["", "## 外部データソース", ""]
    for fr in sol["file_refs"]:
        lines.append(f"- [{fr['id']}] {fr['name']}: {fr['pathList']}"
                     + ("  （DDR に本体あり）" if fr["link"] else ""))
    lines += ["", "## テーマ", "",
              ", ".join(t["name"] for t in sol["themes"]) or "-"]
    if sol["base_dirs"]:
        lines += ["", "## 基準ディレクトリ", ""]
        for d in sol["base_dirs"]:
            lines.append(f"- [{d['id']}] {d['path']}")
    w.write(f"{base}/FileInfo.md", lines)

    # handover が使う「ファイル全体の事実」も索引に残す
    idx["options"] = sol["options"]
    idx["relationships"] = [
        {"id": r["id"],
         "left": r["left"].get("name"), "right": r["right"].get("name"),
         "predicates": r["predicates"],
         "cascade": {side: [k for k in ("cascadeCreate", "cascadeDelete")
                            if r[side].get(k)] for side in ("left", "right")}}
        for r in sol["relationships"]]
    idx["accounts"] = sol["accounts"]
    idx["privilege_sets"] = [
        {k: p[k] for k in ("name", "comment", "menu", "printing", "exporting",
                           "manageAccounts", "areas", "tables")}
        for p in sol["privilege_sets"]]
    idx["ext_privileges"] = sol["ext_privileges"]
    idx["file_refs"] = sol["file_refs"]
    idx["themes"] = [t["name"] for t in sol["themes"]]

    idx["counts"] = {
        "base_tables": len(sol["base_tables"]),
        "fields": sum(len(b["fields"]) for b in sol["base_tables"]),
        "scripts": len(sol["scripts"]),
        "script_steps": sum(s["steps"] for s in sol["scripts"]),
        "layouts": len(sol["layouts"]),
        "table_occurrences": len(sol["tos"]),
        "relationships": len(sol["relationships"]),
        "valuelists": len(sol["valuelists"]),
        "custom_functions": len(sol["custom_functions"]),
        "custom_menus": len(sol["custom_menus"]),
        "menu_sets": len(sol["menu_sets"]),
        "accounts": len(sol["accounts"]),
        "privilege_sets": len(sol["privilege_sets"]),
        "external_files": len(sol["file_refs"]),
    }

    # --- ソリューションの README
    c = idx["counts"]
    lines = [f"# {sol['file_name']}", "",
             "FileMaker DDR を fmddr で分解した結果。", "",
             "| 種別 | 件数 | 場所 |", "|---|---|---|",
             f"| スクリプト | {c['scripts']}（{c['script_steps']} ステップ） | `Scripts/` |",
             f"| テーブル定義 | {c['base_tables']}（{c['fields']} フィールド） | `BaseTables/` |",
             f"| レイアウト | {c['layouts']} | `Layouts/` |",
             f"| TO / リレーション | {c['table_occurrences']} / {c['relationships']} | `Relationships.md` |",
             f"| 値一覧 | {c['valuelists']} | `ValueLists.md` |",
             f"| カスタム関数 | {c['custom_functions']} | `CustomFunctions.md` |",
             f"| カスタムメニュー / セット | {c['custom_menus']} / {c['menu_sets']} | `CustomMenus.md` |",
             f"| アカウント / 権限セット | {c['accounts']} / {c['privilege_sets']} | `Security.md` |",
             f"| 外部データソース | {c['external_files']} | `FileInfo.md` |",
             "", "## 調べ方", "",
             "- 索引: `../index.json`（全要素の一覧）、`../xref.json`（相互参照）",
             "- 相互参照: `fmddr.py xref -d <出力先> --field \"TOP::顧客ID\"`",
             "- 未使用の洗い出し: `fmddr.py audit -d <出力先>`",
             "- 全文検索: `grep -rl \"顧客ID\" Scripts/ Layouts/ BaseTables/`"]
    w.write(f"{base}/README.md", lines)
    return idx


# ---------------------------------------------------------------------------
# 8. サブコマンド
# ---------------------------------------------------------------------------

def cmd_probe(args):
    info = detect_type(args.input)
    kind = {"Summary": "DDR 概要ファイル", "Report": "DDR 本体"}.get(
        info.get("type"), info.get("type") or "?")
    if info["root"] == "FMSaveAsXML":
        kind = "SaveAsXML（DDR ではない）"
    print(f"入力      : {args.input}")
    print(f"ルート要素: {info['root']}")
    print(f"種別      : {kind}")
    print(f"FileMaker : {info.get('version')}")
    print(f"出力日時  : {info.get('creationDate')} {info.get('creationTime')}")
    size = os.path.getsize(args.input) / 1024 / 1024
    print(f"サイズ    : {size:.1f} MB")
    if info["files"]:
        print(f"\n構成ファイル ({len(info['files'])}):")
        base = os.path.dirname(os.path.abspath(args.input))
        for f in info["files"]:
            resolved = resolve_link(base, f.get("link") or "")
            mark = "" if resolved or not f.get("link") else "  ← リンク解決できず"
            print(f"  - {f['name']}{mark}")
            if f["counts"]:
                print("      " + ", ".join(f"{k}:{v}" for k, v in f["counts"].items()))


def cmd_split(args):
    info, targets = input_files(args.input)
    out = os.path.abspath(args.out)
    prev = []
    marker = os.path.join(out, MANIFEST)
    if os.path.isdir(out) and os.path.exists(marker):
        try:
            prev = json.load(open(marker, encoding="utf-8")).get("files", [])
        except (json.JSONDecodeError, OSError):
            prev = []
    os.makedirs(out, exist_ok=True)
    w = Writer(out)

    solutions, xrefs = [], {}
    used_dirs: set[str] = set()
    for xml_path in targets:
        t0 = datetime.now()
        sol = parse_solution(xml_path, args)
        xref = build_xref(sol)
        stem = re.sub(r"\.fmp12$", "", sol["file_name"] or os.path.basename(xml_path))
        base = safe_filename(stem, used_dirs, "")
        idx = write_solution(sol, xref, w, base, args)
        idx["dir"] = base
        solutions.append(idx)
        xrefs[base] = xref  # キーは出力ディレクトリ名（index.json の "dir"）
        dt = (datetime.now() - t0).total_seconds()
        print(f"✓ {sol['file_name']}  "
              f"スクリプト {idx['counts']['scripts']} / テーブル {idx['counts']['base_tables']} /"
              f" レイアウト {idx['counts']['layouts']}  ({dt:.1f}s)")

    index = {"fmddr": VERSION,
             "generated": datetime.now().isoformat(timespec="seconds"),
             "input": os.path.abspath(args.input),
             "ddr": {k: info.get(k) for k in ("type", "version", "creationDate")},
             "solutions": solutions}
    w.write("index.json", json.dumps(index, ensure_ascii=False, indent=1))
    w.write("xref.json", json.dumps(xrefs, ensure_ascii=False, indent=1))

    total = {}
    for s in solutions:
        for k, v in s["counts"].items():
            total[k] = total.get(k, 0) + v
    lines = ["# DDR 解析結果", "",
             f"- 入力: `{os.path.abspath(args.input)}`",
             f"- 解析: fmddr {VERSION} / {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"- DDR: FileMaker {info.get('version')} / {info.get('creationDate')}",
             "", "## ファイル", ""]
    for s in solutions:
        c = s["counts"]
        lines.append(f"- [{s['file_name']}]({s['dir']}/README.md) — "
                     f"スクリプト {c['scripts']} / テーブル {c['base_tables']} / "
                     f"レイアウト {c['layouts']} / 値一覧 {c['valuelists']}")
    lines += ["", "## 合計", "",
              ", ".join(f"{k}: {v}" for k, v in sorted(total.items())),
              "", "## 索引", "",
              "- `index.json` — 全要素の一覧（名前・ID・ファイルパス・参照）",
              "- `xref.json` — 相互参照（フィールド/スクリプト/レイアウト/TO/値一覧の逆引き）",
              "", "## 注意", "",
              "スクリプトのコメントやアカウント一覧には実在の顧客名・担当者名が含まれることがあります。",
              "外部サービスへの貼り付けや公開リポジトリへのコミットは事前に確認してください。"]
    w.write("README.md", lines)

    manifest = {"fmddr": VERSION, "generated": index["generated"],
                "input": index["input"], "files": sorted(w.written)}
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    removed = 0
    if args.clean and prev:
        keep = set(w.written)
        for rel in prev:
            if rel in keep:
                continue
            full = os.path.join(out, rel)
            if os.path.isfile(full):
                os.remove(full)
                removed += 1
        for dirpath, dirnames, filenames in os.walk(out, topdown=False):
            if not dirnames and not filenames:
                os.rmdir(dirpath)
    print(f"\n出力先: {out}")
    print(f"生成ファイル: {len(w.written)}" + (f" / 削除: {removed}" if removed else ""))
    print("最初に読むファイル: README.md（全体） / index.json（要素一覧） / xref.json（相互参照）")


# --- パス式マッチ（nodes / extract 用） ------------------------------------

def compile_path(spec: str) -> tuple[re.Pattern, tuple[str, str] | None]:
    """"/FMPReport/File/ScriptCatalog/Script:name=X" をマッチャに変換する。

    `*` は中間階層を任意個マッチする。属性条件は末尾の :attr=value。
    """
    attr = None
    if ":" in spec:
        head, _, cond = spec.rpartition(":")
        if "=" in cond and "/" not in cond:
            spec = head
            key, _, val = cond.partition("=")
            attr = (key.strip(), val.strip().strip('"\''))
    segs = [s for s in spec.strip("/").split("/") if s]
    parts = []
    for s in segs:
        parts.append(r"(?:/[^/]+)*" if s == "*" else "/" + re.escape(s))
    return re.compile("^" + "".join(parts) + "$"), attr


def iter_matches(xml_path: str, spec: str, non_empty: bool = False):
    """パス式にマッチする要素を yield する（絶対パス, 要素）。

    メモリは「FMPReport/File/<カタログ>/<項目>」の階層（深さ 4）を通過するたびに
    解放して抑える。呼び出し側は受け取った要素を clear してはいけない
    （入れ子の一致があるとき、外側の要素の情報を壊してしまう）。
    """
    if not os.path.isfile(xml_path):
        raise SystemExit(f"ファイルが見つかりません: {xml_path}")
    rx, attr = compile_path(spec)
    clear_depth = 4
    stack: list[str] = []
    for event, el in ET.iterparse(xml_path, events=("start", "end")):
        if event == "start":
            stack.append(el.tag)
            continue
        path = "/" + "/".join(stack)
        depth = len(stack)
        stack.pop()
        if rx.match(path) and not (attr and (el.get(attr[0]) or "") != attr[1]) \
                and not (non_empty and len(el) == 0):
            yield path, el
        if depth == clear_depth:
            el.clear()


def inner_match(el, spec: str) -> bool:
    """--inner のパス式（子孫のみ対象、属性省略可）に合致するか。"""
    rx, attr = compile_path(spec)
    root = "/" + el.tag

    def walk(node, path):
        for child in node:
            cpath = path + "/" + child.tag
            if rx.match(cpath) or rx.match("/" + "/".join(cpath.split("/")[2:])):
                if not attr or (child.get(attr[0]) or "") == attr[1]:
                    return True
            if walk(child, cpath):
                return True
        return False

    return walk(el, root)


def cmd_nodes(args):
    tags: dict[str, int] = {}
    attrs: dict[str, dict[str, str]] = {}
    count = 0
    for path, el in iter_matches(args.input, args.path, args.non_empty):
        count += 1
        for k, v in el.attrib.items():
            attrs.setdefault("(自身の属性)", {}).setdefault(k, v)
        for child in el:
            key = child.tag
            tags[key] = tags.get(key, 0) + 1
            for k, v in child.attrib.items():
                attrs.setdefault(key, {}).setdefault(k, v)
        if count >= args.limit:
            break
    if not count:
        print(f"一致なし: {args.path}\n"
              "→ パス式の誤りとは限りません（その要素がこのファイルに存在しない場合も）。"
              "上位のパスで nodes を実行して実際の構造を確認してください。")
        return
    print(f"一致要素: {count} 件（先頭 {min(count, args.limit)} 件を集計）\n")
    own = attrs.get("(自身の属性)")
    if own:
        print("- (自身の属性)")
        for k, v in own.items():
            print(f"    @{k} = {one_line(v, 60)}")
    for key, n in sorted(tags.items(), key=lambda kv: -kv[1]):
        print(f"- {key}  ×{n}")
        for k, v in (attrs.get(key) or {}).items():
            print(f"    @{k} = {one_line(v, 60)}")


def cmd_extract(args):
    out = []
    n = 0
    for path, el in iter_matches(args.input, args.path, args.non_empty):
        if args.inner and not inner_match(el, args.inner):
            continue
        n += 1
        out.append(ET.tostring(el, encoding="unicode"))
        if n >= args.limit:
            break
    if not n:
        print(f"一致なし: {args.path}"
              + (f" / --inner {args.inner}" if args.inner else "")
              + "\n→ nodes サブコマンドで実際の構造を確認してください。", file=sys.stderr)
        return 1
    body = "\n".join(out)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        print(f"{n} 件を書き出しました: {args.out}")
    else:
        print(body)
    return 0


def load_index(d: str) -> tuple[dict, dict]:
    root = os.path.abspath(d)
    try:
        index = json.load(open(os.path.join(root, "index.json"), encoding="utf-8"))
        xref = json.load(open(os.path.join(root, "xref.json"), encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"{root} に index.json / xref.json がありません。"
                         "先に split を実行してください。")
    return index, xref


def cmd_xref(args):
    index, xrefs = load_index(args.dir)
    buckets = {"field": "fields", "script": "scripts", "layout": "layouts",
               "to": "table_occurrences", "table": "base_tables",
               "valuelist": "valuelists"}
    query = None
    bucket = None
    for opt, key in buckets.items():
        val = getattr(args, opt)
        if val:
            query, bucket = val, key
            break
    if query is None:
        raise SystemExit("--field / --script / --layout / --to / --table / --valuelist "
                         "のいずれかを指定してください。")
    hits = 0
    for file_name, xref in xrefs.items():
        data = xref.get(bucket, {})
        keys = [k for k in data if k == query] or \
               [k for k in data if query.casefold() in k.casefold()]
        for k in keys[:args.limit]:
            hits += 1
            print(f"\n=== {file_name} / {bucket}: {k}")
            where = data[k]
            if not where:
                print("  参照なし（定義のみ）")
            for kind, vals in where.items():
                print(f"  {kind} ({len(vals)}): {', '.join(vals[:60])}")
    if not hits:
        print(f"{bucket} に「{query}」は見つかりませんでした。")


AUDIT_SECTIONS = [
    ("no_caller", "呼び出し元が見つからないスクリプト", "scripts",
     "メニューからの手動実行・スクリプト名を計算で組み立てる呼び出しは検出できません。"),
    ("unused_valuelists", "使用箇所が見つからない値一覧", "valuelists", ""),
    ("unused_layouts", "参照されないレイアウト", "layouts",
     "レイアウトメニューから手動で切り替えるものは参照なしと出ます。"),
    ("unused_tos", "どこからも使われない TO", "table_occurrences", ""),
    ("unused_fields", "どこからも参照されないフィールド", "fields",
     "TO 名を計算で組み立てる参照・外部ファイルからの参照は検出できません。"),
]


def audit_lists(sol: dict, xref: dict) -> dict:
    """未使用候補を洗い出す。audit と handover で共用する。"""
    no_caller = [s["name"] for s in sol["scripts"]
                 if not any(k in (xref.get("scripts", {}).get(s["name"]) or {})
                            for k in ("called_by", "layouts", "custom_menus",
                                      "file_triggers"))]
    unused_vl = [v["name"] for v in sol["valuelists"]
                 if not xref.get("valuelists", {}).get(v["name"])]
    unused_lay = [l["name"] for l in sol["layouts"]
                  if not xref.get("layouts", {}).get(l["name"])]
    unused_to = [t["name"] for t in sol["table_occurrences"]
                 if not xref.get("table_occurrences", {}).get(t["name"])]

    # 参照されていないフィールド（レイアウト・スクリプト・計算式・リレーション）
    used_fields = xref.get("fields", {})
    unused_fields = []
    for bt in sol["base_tables"]:
        occ = xref.get("base_tables", {}).get(bt["name"], {}).get("occurrences", [])
        for fname in bt["field_names"]:
            keys = {f"{bt['name']}::{fname}"} | {f"{o}::{fname}" for o in occ}
            if not any(used_fields.get(k) for k in keys):
                unused_fields.append(f"{bt['name']}::{fname}")

    return {"no_caller": no_caller, "unused_valuelists": unused_vl,
            "unused_layouts": unused_lay, "unused_tos": unused_to,
            "unused_fields": unused_fields,
            "totals": {"scripts": len(sol["scripts"]),
                       "valuelists": len(sol["valuelists"]),
                       "layouts": len(sol["layouts"]),
                       "table_occurrences": len(sol["table_occurrences"]),
                       "fields": sol["counts"]["fields"]}}


def cmd_audit(args):
    index, xrefs = load_index(args.dir)
    for sol in index["solutions"]:
        a = audit_lists(sol, xrefs.get(sol["dir"], {}))
        print(f"\n=== {sol['file_name']}")
        for key, label, total_key, note in AUDIT_SECTIONS:
            items = a[key]
            print(f"\n[{label}] {len(items)}/{a['totals'][total_key]}")
            for n in items[:args.limit]:
                print(f"  - {n}")
            if len(items) > args.limit:
                print(f"  … 他 {len(items) - args.limit} 件")
            if note:
                print(f"  ※ {note}")


# ---------------------------------------------------------------------------
# 8.5 引き継ぎ資料の骨組み生成（handover）
# ---------------------------------------------------------------------------

TODO_MARK = "要執筆"

# スクリプト本文から拾う「何をしているか」の手掛かり。
# 日本語 DDR のステップ名を前提にしつつ、英語 DDR も拾えるように併記する。
STEP_SIGNALS = [
    ("外部ファイルを開く", r"ファイルを開く \[|Open File \["),
    ("外部ファイルのスクリプトを実行", r"スクリプト実行 \[[^\]]*ファイル:|from file:"),
    ("URL を開く / API 呼び出し", r"URL を開く \[|URL から挿入 \[|Insert from URL|Open URL \["),
    ("メール送信", r"メールを送信 \[|Send Mail \["),
    ("インポート", r"レコードのインポート \[|Import Records \["),
    ("エクスポート", r"レコードのエクスポート \[|フィールド内容のエクスポート \[|Export Records \["),
    ("PDF / Excel 出力", r"として保存 \[|Save Records as"),
    ("印刷", r"^\s*(?://\s*)?印刷 \[|^\s*(?://\s*)?Print \["),
    ("ExecuteSQL", r"ExecuteSQL"),
    ("スクリプト名を計算で組み立てる呼び出し", r"名前で指定|Perform Script by Name"),
    ("フィールド名・レイアウト名の動的指定", r"GetField \(|Evaluate \(|Set Field By Name"),
]

# 改修前に目を通しておくべき箇所。値そのものは資料に出さない（件数と場所だけ）。
RISK_SIGNALS = [
    ("平文パスワードの埋め込み", r'パスワード: "[^"]+"|Password: "[^"]+"'),
    ("URL に埋め込まれた認証情報", r"://[^\"\s@/]+:[^\"\s@/]+@"),
    ("SMTP の認証情報", r'認証タイプ: [^;\]]*; ユーザ名: "'),
    ("API キー・トークンらしい記述", r"(?i)api[_-]?key|bearer |access[_-]?token|client[_-]?secret"),
    ("完全アクセス権で実行", r"^- .*完全アクセス権で実行: True"),
]

# 旧版の残骸・作業途中を名前から拾う（命名に依存する推測）
LEGACY_WORDS = ("旧", "old", "コピー", "copy", "テスト", "test", "バックアップ",
                "backup", "bak", "予備", "使わない", "未使用", "不要", "削除",
                "作成中", "not use", "仮")

# 帳票らしいレイアウトを名前から拾う（同じく推測）
FORM_WORDS = ("印刷", "print", "帳票", "PDF", "納品書", "請求書", "見積書",
              "注文書", "検収書", "送付状", "封筒", "ラベル", "報告書")

# ---------------------------------------------------------------------------
# 共有ヘルパー（estimate-docs skill もここから取る。重複定義を作らないこと）
# ---------------------------------------------------------------------------

# 整形本文では 1 ステップが必ず字下げ付きの行として始まり、計算式の折り返しは
# 字下げ無しで続く。「字下げがあってコメントでない行」＝実処理ステップ数。
RE_STEP_LINE = re.compile(r"^  +[^#\s]", re.M)

# 結合フィールド名がキーらしいか（リレーションの多重度推定に使う）
KEYISH = re.compile(r"(^|[_\s])(主キー|primary|pk|ID|id|key|コード|CD)$", re.I)

# ステップ名 → 「移行時に代替実装の検討が必要な固有機能」
STEP_FEATURES = {
    "URL から挿入": "外部API連携",
    "メールを送信": "メール送信",
    "レコードを PDF として保存": "PDF出力",
    "レコードを Excel として保存": "Excel出力",
    "レコードのインポート": "データインポート",
    "レコードのエクスポート": "データエクスポート",
    "フィールド内容のエクスポート": "添付ファイル出力",
    "ファイルを挿入": "添付ファイル取込",
    "印刷": "印刷",
    "印刷設定": "印刷",
    "レコードのソート": "ソート",
    "検索実行": "検索",
    "検索モードに切り替え": "検索",
    "クイック検索の実行": "検索",
    "検索条件を変更": "検索",
    "対象レコードの絞り込み": "検索",
    "対象外のみを表示": "検索",
    "Web ビューアで JavaScript を実行": "Webビューア連携",
    "ファイルを開く": "外部ファイル参照",
    "再ログイン": "認証",
    "Event を送信": "外部アプリ連携",
    "ポータル内の行を削除": "ポータル操作(子テーブル明細)",
    "ポータルの更新": "ポータル操作(子テーブル明細)",
    "ポータル内の行へ移動": "ポータル操作(子テーブル明細)",
    "新規ウインドウ": "複数ウインドウ制御",
    "ウインドウを閉じる": "複数ウインドウ制御",
    "ウインドウの固定": "複数ウインドウ制御",
    "ウインドウを選択": "複数ウインドウ制御",
    "Loop": "繰り返し処理",
    "スクリプト実行": "サブスクリプト呼び出し",
    "関連レコードへ移動": "関連レコード遷移",
    "カスタムダイアログを表示": "ダイアログ",
    # 英語 DDR 用の別名
    "Insert from URL": "外部API連携", "Send Mail": "メール送信",
    "Save Records as PDF": "PDF出力", "Save Records as Excel": "Excel出力",
    "Import Records": "データインポート", "Export Records": "データエクスポート",
    "Export Field Contents": "添付ファイル出力", "Insert File": "添付ファイル取込",
    "Print": "印刷", "Print Setup": "印刷", "Sort Records": "ソート",
    "Perform Find": "検索", "Enter Find Mode": "検索", "Perform Quick Find": "検索",
    "Constrain Found Set": "検索", "Show Omitted Only": "検索",
    "Perform JavaScript in Web Viewer": "Webビューア連携",
    "Open File": "外部ファイル参照", "Re-Login": "認証", "Send Event": "外部アプリ連携",
    "Delete Portal Row": "ポータル操作(子テーブル明細)",
    "Refresh Portal": "ポータル操作(子テーブル明細)",
    "Go to Portal Row": "ポータル操作(子テーブル明細)",
    "New Window": "複数ウインドウ制御", "Close Window": "複数ウインドウ制御",
    "Freeze Window": "複数ウインドウ制御", "Select Window": "複数ウインドウ制御",
    "Perform Script": "サブスクリプト呼び出し",
    "Go to Related Record": "関連レコード遷移",
    "Show Custom Dialog": "ダイアログ",
}

# 整形本文からステップ名の見出しだけを取り出す
RE_STEP_HEAD = re.compile(r"^  +([^#\s][^\[\n]*?)\s*(?:\[|$)", re.M)


def real_steps(body: str) -> int:
    """コメント・空行を除いた実処理ステップ数。ロジック量の目安。

    DDR のステップ総数にはコメントと空コメントが混ざっているので、そのまま数えると
    実装ボリュームを 1.5〜2 倍に見せてしまう。
    """
    return len(RE_STEP_LINE.findall(body or ""))


def step_features(body: str) -> list[str]:
    """整形本文から固有機能を拾う。"""
    return sorted({STEP_FEATURES[s.strip()] for s in RE_STEP_HEAD.findall(body or "")
                   if s.strip() in STEP_FEATURES})


def infer_cardinality(predicates: list[dict]) -> str:
    """リレーションの多重度を結合述語から推定する。

    FileMaker は基数を宣言しないので、等結合の右側／左側がキーらしい名前かで見る。
    あくまで推定であって保証ではない。
    """
    card = "多対多"
    for p in predicates or []:
        if (p.get("type") or p.get("op")) not in ("Equal", "=", None):
            continue
        if KEYISH.search((p.get("right") or "").split("::")[-1]):
            card = "1対多"
        elif KEYISH.search((p.get("left") or "").split("::")[-1]):
            card = "多対1"
    return card


def mm_id(name: str) -> str:
    """Mermaid のノード ID。記号を落として英数字と _ だけにする。"""
    s = re.sub(r"[^0-9A-Za-z一-龥ぁ-んァ-ヶー_]", "_", name or "")
    return re.sub(r"_+", "_", s).strip("_") or "T"


def mm_label(name: str) -> str:
    """Mermaid のラベル。図を壊す記号を全角に逃がす。"""
    return (name or "").replace('"', "'").replace("[", "［").replace("]", "］") \
                       .replace("<", "＜").replace(">", "＞").replace("|", "｜") \
                       .replace("{", "｛").replace("}", "｝")


def _todo(title: str, ask: str, hints=None) -> list[str]:
    """執筆枠。`grep -n 要執筆` で残りを数えられるようにマーカーを入れる。"""
    out = [f"> **{TODO_MARK}｜{title}**", f"> {ask}"]
    if hints:
        out.append("> ")
        out.append("> 手掛かり: " + " / ".join(f"`{h}`" for h in hints))
    return out + [""]


def _table(head: list[str], rows: list[list], limit: int = 0) -> list[str]:
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    shown = rows[:limit] if limit else rows
    for r in shown:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    if limit and len(rows) > limit:
        out.append(f"| … 他 {len(rows) - limit} 件 | " +
                   " | ".join("" for _ in head[1:]) + " |")
    return out + [""]


def _names(items, limit=30) -> str:
    items = list(items)
    if not items:
        return "-"
    more = f" …他 {len(items) - limit} 件" if len(items) > limit else ""
    return ", ".join(str(i) for i in items[:limit]) + more


def _has_legacy(name: str) -> bool:
    low = (name or "").casefold()
    return any(w.casefold() in low for w in LEGACY_WORDS)


class Handover:
    """split 済みの索引から、引き継ぎ資料の「事実パート」を組み立てる。"""

    def __init__(self, sol: dict, xref: dict, split_root: str, out_dir: str, opts):
        self.sol = sol
        self.xref = xref
        self.split_root = split_root
        self.opts = opts
        # 資料から split 出力を参照するための相対パス
        self.rel = os.path.relpath(split_root, out_dir).replace(os.sep, "/")
        self.to2bt = {t["name"]: t["base_table"] for t in sol["table_occurrences"]}
        self.by_name = {s["name"]: s for s in sol["scripts"]}
        self.audit = audit_lists(sol, xref)
        self._bodies = None

    # --- 素材 -------------------------------------------------------------

    def link(self, path: str) -> str:
        """split 出力内のファイルへのリンク（資料からの相対パス）。"""
        return f"{self.rel}/{path}"

    @property
    def bodies(self) -> dict:
        """Scripts/*.md の本文。ステップ名の検出に使う（遅延読み込み）。

        **キーは name ではなく path。** 同名スクリプトが複数ある DDR が実在するので
        （この DDR にも「新規スクリプト」が 7 件ある）、name で持つと潰れる。
        """
        if self._bodies is None:
            self._bodies = {}
            for s in self.sol["scripts"]:
                try:
                    with open(os.path.join(self.split_root, s["path"]),
                              encoding="utf-8") as fh:
                        self._bodies[s["path"]] = fh.read()
                except OSError:
                    pass
        return self._bodies

    def body_of(self, script: dict) -> str:
        return self.bodies.get(script["path"], "")

    def scripts_matching(self, pattern: str) -> list[str]:
        """パターンに当たるスクリプト名（表示用なので同名は 1 つに束ねる）。"""
        pat = re.compile(pattern, re.M)
        return sorted({s["name"] for s in self.sol["scripts"]
                       if pat.search(self.body_of(s))})

    def script_xref(self, name: str) -> dict:
        return self.xref.get("scripts", {}).get(name) or {}

    def entrypoints(self) -> list[tuple[str, str, str]]:
        """(区分, 呼び出し口, スクリプト名) の一覧。

        レイアウトトリガは同じスクリプトが何十画面にも仕掛けられていることが
        あるので、(スクリプト, イベント) でまとめて画面名は代表だけ出す。
        """
        out = []
        for ev, scr in (self.sol["options"].get("triggers") or {}).items():
            out.append(("ファイルトリガ", ev, scr))
        for s in self.sol["scripts"]:
            w = self.script_xref(s["name"])
            if w.get("custom_menus"):
                out.append(("カスタムメニュー", _names(w["custom_menus"], 6), s["name"]))
        grouped: dict[tuple[str, str], list[str]] = {}
        for lay in self.sol["layouts"]:
            for t in lay["triggers"]:
                if t["script"]:
                    grouped.setdefault((t["script"], t["event"]), []).append(lay["name"])
        for (scr, ev), lays in sorted(grouped.items(), key=lambda x: -len(x[1])):
            where = f"{ev}（{len(lays)} 画面: {_names(sorted(lays), 5)}）" \
                if len(lays) > 1 else f"{lays[0]} / {ev}"
            out.append(("レイアウトトリガ", where, scr))
        return out

    def call_tree(self, root: str, depth: int = 3, width: int = 10) -> list[str]:
        lines, seen = [], set()

        def walk(name: str, level: int):
            pad = "  " * level
            if name in seen:
                lines.append(f"{pad}- {name} …（既出）")
                return
            seen.add(name)
            lines.append(f"{pad}- {name}")
            if level >= depth:
                return
            s = self.by_name.get(name)
            kids = sorted((s or {}).get("refs", {}).get("scripts") or [])
            for k in kids[:width]:
                walk(k, level + 1)
            if len(kids) > width:
                lines.append(f"{pad}  - …他 {len(kids) - width} 件")

        walk(root, 0)
        return lines

    def er_pairs(self) -> dict[tuple[str, str], dict]:
        """BaseTable 単位に集約したリレーションのペア。

        TO をそのまま出すと 300 超のノードになって図が読めないので、
        TO → BaseTable に畳んで本数と多重度をまとめる。
        """
        pairs: dict[tuple[str, str], dict] = {}
        for r in self.sol["relationships"]:
            a = self.to2bt.get(r["left"]) or r["left"]
            b = self.to2bt.get(r["right"]) or r["right"]
            if not a or not b or a == b:
                continue
            key = tuple(sorted([a, b]))
            slot = pairs.setdefault(key, {"n": 0, "card": "多対多", "tos": set()})
            slot["n"] += 1
            slot["tos"].add(f"{r['left']} — {r['right']}")
            if infer_cardinality(r["predicates"]) in ("1対多", "多対1"):
                slot["card"] = "1対多"
        return pairs

    def er_islands(self) -> list[list[str]]:
        """BaseTable のリレーショングラフを連結成分（島）に分ける。"""
        adj: dict[str, set[str]] = {b["name"]: set() for b in self.sol["base_tables"]}
        for a, b in self.er_pairs():
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        seen, islands = set(), []
        for start in sorted(adj):
            if start in seen:
                continue
            stack, group = [start], []
            seen.add(start)
            while stack:
                n = stack.pop()
                group.append(n)
                for m in sorted(adj[n]):
                    if m not in seen:
                        seen.add(m)
                        stack.append(m)
            islands.append(sorted(group))
        return sorted(islands, key=lambda g: -len(g))

    def transitions(self) -> list[dict]:
        """画面遷移。レイアウトのボタン／トリガから遷移先を推定する。

        - direct: レイアウト上のステップが直接そのレイアウトへ切り替える
        - via:    レイアウトが呼ぶスクリプトの中で切り替えている（経由スクリプト付き）

        FileMaker はスクリプトから任意のレイアウトへ飛べるので、網羅性は保証できない。
        """
        known = {l["name"] for l in self.sol["layouts"]}
        out, seen = [], set()
        for lay in self.sol["layouts"]:
            src = lay["name"]
            for dst in sorted(lay["refs"].get("layouts") or []):
                if dst in known and dst != src and (src, dst, None) not in seen:
                    seen.add((src, dst, None))
                    out.append({"src": src, "dst": dst, "kind": "direct", "via": None})
            for scr in sorted(lay["refs"].get("scripts") or []):
                s = self.by_name.get(scr)
                if not s:
                    continue
                for dst in sorted(s["refs"].get("layouts") or []):
                    if dst in known and dst != src and (src, dst, scr) not in seen:
                        seen.add((src, dst, scr))
                        out.append({"src": src, "dst": dst, "kind": "via", "via": scr})
        return out

    def dup_names(self, key: str) -> list[tuple[str, int]]:
        seen: dict[str, int] = {}
        for item in self.sol[key]:
            seen[item["name"]] = seen.get(item["name"], 0) + 1
        return sorted(((n, c) for n, c in seen.items() if c > 1),
                      key=lambda x: -x[1])

    # --- 章 ---------------------------------------------------------------

    def ch_overview(self) -> list[str]:
        s, c, o = self.sol, self.sol["counts"], self.sol["options"]
        big_tables = sorted(s["base_tables"], key=lambda b: -b["fields"])[:10]
        big_scripts = sorted(s["scripts"], key=lambda x: -x["steps"])[:10]
        big_layouts = sorted(s["layouts"], key=lambda x: -x["objects"])[:10]

        out = [f"# 1. システム概要 — {s['file_name']}", "",
               f"- DDR: FileMaker {s['ddr'].get('version')} / "
               f"出力日 {s['ddr'].get('creationDate')}",
               f"- 起動アカウント: {o.get('onOpenAccount') or '-'} / "
               f"起動レイアウト: {o.get('onOpenLayout') or '-'}",
               f"- 最小許容バージョン: {o.get('minVersion') or '-'} / "
               f"既定テーマ: {o.get('defaultTheme') or '-'} / "
               f"既定メニューセット: {o.get('defaultMenuSet') or '-'}"]
        trg = o.get("triggers") or {}
        out.append("- ファイルトリガ: " +
                   (", ".join(f"{k} → 「{v}」" for k, v in trg.items()) or "なし"))
        out += ["", "## 規模", ""]
        out += _table(["種別", "件数"], [
            ["テーブル定義", c["base_tables"]],
            ["フィールド", c["fields"]],
            ["スクリプト", f"{c['scripts']}（{c['script_steps']} ステップ）"],
            ["レイアウト", c["layouts"]],
            ["テーブルオカレンス / リレーション",
             f"{c['table_occurrences']} / {c['relationships']}"],
            ["値一覧", c["valuelists"]],
            ["カスタム関数", c["custom_functions"]],
            ["カスタムメニュー / セット", f"{c['custom_menus']} / {c['menu_sets']}"],
            ["アカウント / 権限セット", f"{c['accounts']} / {c['privilege_sets']}"],
            ["外部データソース", c["external_files"]],
        ])
        out += ["## 規模の大きい要素（読む順番の目安）", "",
                "### フィールド数の多いテーブル", ""]
        out += _table(["テーブル", "フィールド", "レコード"],
                      [[b["name"], b["fields"], b["records"]] for b in big_tables])
        out += ["### ステップ数の多いスクリプト", ""]
        out += _table(["スクリプト", "ステップ", "無効"],
                      [[x["name"], x["steps"], x["disabled"]] for x in big_scripts])
        out += ["### オブジェクト数の多いレイアウト", ""]
        out += _table(["レイアウト", "TO", "オブジェクト"],
                      [[x["name"], x["to"] or "-", x["objects"]] for x in big_layouts])
        out += _todo("システムの目的",
                     "このシステムが何をするためのものか、扱う業務と主要な登場人物を "
                     "3〜5 行で。上の規模表と起動レイアウトから当たりを付け、"
                     "起動スクリプトと TOP 画面を読んで裏を取る。",
                     [self.link(f"{s['dir']}/Layouts/"),
                      self.link(f"{s['dir']}/Scripts/"),
                      self.link(f"{s['dir']}/README.md")])
        out += _todo("設計の癖",
                     "命名規則（テーブル名の接尾辞、g_ で始まるグローバル、"
                     "sub_ の意味など）と、このファイル特有の約束事を箇条書きで。",
                     [self.link(f"{s['dir']}/BaseTables/")])
        return out

    def ch_tables(self) -> list[str]:
        s = self.sol
        lay_by_bt: dict[str, int] = {}
        for lay in s["layouts"]:
            if lay["base_table"]:
                lay_by_bt[lay["base_table"]] = lay_by_bt.get(lay["base_table"], 0) + 1
        scr_by_bt: dict[str, int] = {}
        for scr in s["scripts"]:
            touched = {self.to2bt.get(k.split("::")[0])
                       for k in (scr["refs"].get("fields") or [])}
            for bt in touched - {None}:
                scr_by_bt[bt] = scr_by_bt.get(bt, 0) + 1

        rows = []
        for b in sorted(s["base_tables"], key=lambda x: -x["fields"]):
            occ = self.xref.get("base_tables", {}).get(b["name"], {}).get("occurrences") or []
            rows.append([f"[{b['name']}]({self.link(b['path'])})", b["fields"],
                         b["records"], len(occ), lay_by_bt.get(b["name"], 0),
                         scr_by_bt.get(b["name"], 0),
                         _names(b["field_names"][:6], 6)])
        out = [f"# 2. テーブル構成 — {s['file_name']}", "",
               f"- テーブル {s['counts']['base_tables']} / "
               f"フィールド {s['counts']['fields']}",
               "- 「TO」はこのテーブルのテーブルオカレンス数、"
               "「画面」「スクリプト」はそれを参照している数", ""]
        out += _table(["テーブル", "フィールド", "レコード", "TO", "画面",
                       "スクリプト", "先頭フィールド"], rows)
        out += _todo("各テーブルの役割",
                     "上の表に「役割」列を足す形で、テーブルごとに 1 行の説明を書く。"
                     "フィールド数・レコード数・参照数が 0 に近いものは "
                     "9 章の棚卸しと突き合わせる。",
                     [self.link(f"{s['dir']}/BaseTables/")])
        out += _todo("マスタとトランザクションの区別",
                     "マスタ系・トランザクション系・作業用（Temporary / グローバル置き場）"
                     "に分類し、どれが業務データの本体かを明示する。")
        return out

    def ch_relations(self) -> list[str]:
        s = self.sol
        deg: dict[str, int] = {}
        for r in s["relationships"]:
            for side in ("left", "right"):
                if r[side]:
                    deg[r[side]] = deg.get(r[side], 0) + 1
        ext = [t for t in s["table_occurrences"] if t["file"]]

        out = [f"# 3. リレーション — {s['file_name']}", "",
               f"- TO {s['counts']['table_occurrences']} / "
               f"リレーション {s['counts']['relationships']}",
               f"- 全リレーションの述語一覧は [Relationships.md]"
               f"({self.link(s['dir'] + '/Relationships.md')}) にある", "",
               "## ハブになっている TO（リレーション数の多い順）", ""]
        out += _table(["TO", "BaseTable", "リレーション数"],
                      [[n, self.to2bt.get(n, "-"), c]
                       for n, c in sorted(deg.items(), key=lambda x: -x[1])[:20]])
        out += ["## 外部ファイルの TO", ""]
        if ext:
            out += _table(["TO", "参照先ファイル", "BaseTable"],
                          [[t["name"], t["file"], t["base_table"] or "-"] for t in ext])
        else:
            out += ["（このファイルに外部ファイル参照の TO はありません）", ""]
        out += ["## 連鎖削除が設定されているリレーション", ""]
        casc = [[r["left"], r["right"],
                 "左" if "cascadeDelete" in r["cascade"]["left"] else "右"]
                for r in s["relationships"]
                if "cascadeDelete" in r["cascade"]["left"]
                or "cascadeDelete" in r["cascade"]["right"]]
        if casc:
            out += _table(["左 TO", "右 TO", "削除が伝播する側"], casc, limit=40)
            out += ["> 連鎖削除は事故が起きやすい箇所。改修時は必ず確認する。", ""]
        else:
            out += ["（連鎖削除の設定はありません）", ""]
        out += _todo("リレーショングラフの構造",
                     "ハブ TO を起点に、グラフがいくつの島に分かれているか、"
                     "島ごとに何の業務を担っているかを説明する。",
                     [self.link(f"{s['dir']}/Relationships.md")])
        return out

    def ch_flows(self) -> list[str]:
        s = self.sol
        eps = self.entrypoints()
        hub = sorted(((n, len(w.get("called_by") or []))
                      for n, w in self.xref.get("scripts", {}).items()),
                     key=lambda x: -x[1])[:20]

        out = [f"# 4. 主要処理フロー — {s['file_name']}", "",
               f"- スクリプト {s['counts']['scripts']}（{s['counts']['script_steps']} ステップ）",
               "- 「入口」＝ファイルトリガ・カスタムメニュー・レイアウトトリガから"
               "起動されるスクリプト。ボタンからの呼び出しは"
               "各レイアウトの Markdown に出ている", "", "## 入口", ""]
        out += _table(["区分", "呼び出し口", "スクリプト"],
                      [[k, w, f"[{n}]({self.link(self.by_name[n]['path'])})"
                        if n in self.by_name else n] for k, w, n in eps], limit=60)

        trg = (s["options"].get("triggers") or {})
        for ev, scr in trg.items():
            if scr in self.by_name:
                out += [f"## 起動時の呼び出しツリー（{ev} → {scr}）", ""]
                out += self.call_tree(scr) + [""]

        out += ["## よく呼ばれる共通スクリプト", ""]
        out += _table(["スクリプト", "呼び出し元の数"],
                      [[n, c] for n, c in hub if c])
        out += _todo("業務ごとの処理フロー",
                     "主要な業務（例: 受注 → 請求 → 入金）ごとに、"
                     "どの画面からどのスクリプトが動いて何のテーブルが更新されるかを、"
                     "入口ごとに 1 節ずつ書く。呼び出しツリーを起点に、"
                     "各スクリプトの Markdown 冒頭の依存関係を読めば追える。",
                     [self.link(f"{s['dir']}/Scripts/")])
        out += _todo("定期実行・自動処理",
                     "サーバスケジュールや手動の定期作業で動かすスクリプトを列挙する。"
                     "DDR にはスケジュール設定が入らないので、"
                     "FileMaker Server の設定と運用手順を別途確認して補う。")
        return out

    def ch_layouts(self) -> list[str]:
        s = self.sol
        groups: dict[str, list] = {}
        for lay in s["layouts"]:
            groups.setdefault(lay["group"] or "（グループなし）", []).append(lay)

        out = [f"# 5. 画面一覧 — {s['file_name']}", "",
               f"- レイアウト {s['counts']['layouts']}", ""]
        for g, lays in sorted(groups.items()):
            out += [f"## {g}（{len(lays)}）", ""]
            rows = []
            for lay in sorted(lays, key=lambda x: x["name"]):
                w = self.xref.get("layouts", {}).get(lay["name"]) or {}
                rows.append([f"[{lay['name']}]({self.link(lay['path'])})",
                             lay["to"] or "-", lay["objects"],
                             len(lay["portals"]), len(lay["triggers"]),
                             len(w.get("scripts") or [])])
            out += _table(["レイアウト", "TO", "オブジェクト", "ポータル",
                           "トリガ", "参照元スクリプト"], rows)

        forms = [l for l in s["layouts"]
                 if any(x.casefold() in l["name"].casefold() for x in FORM_WORDS)]
        out += ["## 帳票・印刷らしいレイアウト（名前からの推測）", "",
                f"- {len(forms)} 件: {_names((l['name'] for l in forms), 60)}", "",
                "> 名前で拾っているだけなので取りこぼし・誤検出がある。"
                "印刷ステップを持つスクリプトは 4 章と 8 章で確認する。", ""]
        out += _todo("画面の役割と遷移",
                     "グループごとに、どの画面が業務のどの段階で使われるか、"
                     "画面間の遷移（どのボタンでどこへ行くか）を書く。"
                     "各レイアウトの Markdown にボタンの実行内容が出ている。",
                     [self.link(f"{s['dir']}/Layouts/")])
        return out

    def ch_external(self) -> list[str]:
        s = self.sol
        rows = []
        for fr in s["file_refs"]:
            tos = [t["name"] for t in s["table_occurrences"] if t["file"] == fr["name"]]
            opened = self.xref.get("external_files", {}).get(fr["name"], {}).get("scripts") or []
            rows.append([fr["name"], fr["pathList"] or "-", len(tos),
                         _names(opened, 8)])
        out = [f"# 6. 外部連携 — {s['file_name']}", "",
               f"- 外部データソース {s['counts']['external_files']}", "",
               "## 外部データソース", ""]
        out += _table(["データソース", "パス", "TO 数", "開くスクリプト"], rows)

        out += ["## 外部とやりとりするステップを持つスクリプト", ""]
        rows = []
        for label, pat in STEP_SIGNALS:
            hits = self.scripts_matching(pat)
            if hits:
                rows.append([label, len(hits), _names(hits, 12)])
        out += _table(["種別", "件数", "スクリプト"], rows) if rows else \
            ["（外部連携のステップは検出されませんでした）", ""]
        out += ["> ステップ名で拾っているので、無効化されたステップも含む。"
                "件数が合わないときは該当スクリプトの Markdown を直接見る。", ""]
        out += _todo("連携の目的と止まったときの影響",
                     "外部ファイル・API・メール送信それぞれについて、"
                     "何のために繋いでいるか、止まると業務のどこが困るか、"
                     "接続情報の管理場所を書く。",
                     [self.link(f"{s['dir']}/FileInfo.md")])
        return out

    def ch_security(self) -> list[str]:
        s = self.sol
        full = [a["name"] for a in s["accounts"]
                if a["privilegeSet"] == "[完全アクセス]" and a["status"] == "Active"]
        by_set: dict[str, list[str]] = {}
        for a in s["accounts"]:
            by_set.setdefault(a["privilegeSet"], []).append(a["name"])

        out = [f"# 7. 権限マトリクス — {s['file_name']}", "",
               f"- アカウント {s['counts']['accounts']} / "
               f"権限セット {s['counts']['privilege_sets']} / "
               f"テーブル {s['counts']['base_tables']}",
               f"- 完全アクセス権の有効アカウント: {len(full)} 件 — {_names(full)}", "",
               "> **この章は取り扱い注意。** アカウント名は実在のものなので、"
               "資料の配布範囲を決めてから中身を埋める。", "",
               "## テーブル × 権限セット", "",
               "記号の意味: `C` 作成 / `R` 閲覧 / `U` 編集 / `D` 削除 / `-` アクセスなし。",
               "末尾の `*` は**フィールド単位の制限あり**（`FieldAccess=Limited`）。", "",
               "レコードアクセスが `Custom` の権限セットだけがテーブルごとの設定を持つ。"
               "それ以外は全テーブルに同じ設定が効くので、同じ記号が並ぶ。", ""]

        sets = s["privilege_sets"]
        blanket = {"CreateEditDelete": "CRUD", "CreateEdit": "CRU",
                   "ViewOnly": "R", "None": "-", "NoAccess": "-"}

        def cell(p: dict, table: str) -> str:
            rec = (p["areas"].get("Records") or "").split(" ")[0]
            if rec != "Custom":
                return blanket.get(rec, rec or "?")
            row = next((t for t in p["tables"] if t["name"] == table), None)
            if row is None:
                return "?"
            if (row.get("FieldAccess") or "") == "NoAccess" and \
                    not any(is_true(row.get(k)) for k in ("Create", "View", "Edit", "Delete")):
                return "-"
            code = "".join(c for c, k in (("C", "Create"), ("R", "View"),
                                          ("U", "Edit"), ("D", "Delete"))
                           if is_true(row.get(k)))
            mark = "*" if (row.get("FieldAccess") or "") == "Limited" else ""
            return (code or "-") + mark

        rows = [[b["name"]] + [cell(p, b["name"]) for p in sets]
                for b in sorted(s["base_tables"], key=lambda x: x["name"])]
        out += _table(["テーブル"] + [p["name"] for p in sets], rows)

        out += ["## 権限セットの設定", ""]
        out += _table(["権限セット", "使うアカウント", "説明", "レコード", "レイアウト",
                       "値一覧", "スクリプト", "メニュー", "印刷", "エクスポート",
                       "アカウント管理"],
                      [[p["name"], _names(sorted(by_set.get(p["name"]) or []), 8) or "-",
                        p["comment"] or "-",
                        p["areas"].get("Records", "-"), p["areas"].get("Layouts", "-"),
                        p["areas"].get("ValueLists", "-"), p["areas"].get("Scripts", "-"),
                        p["menu"], p["printing"], p["exporting"], p["manageAccounts"]]
                       for p in sets])

        out += ["## アカウント", ""]
        out += _table(["アカウント", "権限セット", "状態", "管理", "空パスワード", "説明"],
                      [[a["name"], a["privilegeSet"], a["status"], a["managedBy"],
                        a["emptyPassword"], a["description"] or "-"]
                       for a in s["accounts"]])

        out += ["## 拡張アクセス権 × 権限セット", ""]
        rows = [[e["name"], e["comment"] or "-"] +
                ["✓" if p["name"] in (e["sets"] or []) else "-" for p in sets]
                for e in s["ext_privileges"]]
        out += _table(["拡張アクセス権", "説明"] + [p["name"] for p in sets], rows)

        unused_set = [p["name"] for p in sets if not by_set.get(p["name"])]
        if unused_set:
            out += ["## 使われていない権限セット", "",
                    f"- {_names(unused_set)}", "",
                    "> 割り当てられたアカウントが無い権限セット。"
                    "外部ファイルからのアクセス用に残していることもある。", ""]

        out += _todo("権限設計の意図",
                     "権限セットごとに「誰が使う想定か」「なぜその範囲か」を書く。"
                     "マトリクスで `-` になっているのに業務上は見えているはずのテーブルが"
                     "あれば、外部ファイル経由で見ているなど理由を補う。")
        out += _todo("アカウント運用のルール",
                     "開発用アカウントの扱い、退職時の手順、完全アクセス権を渡す条件を書く。"
                     "空パスワードや共有アカウントがあれば、その理由と是正方針も。")
        return out

    def ch_risks(self) -> list[str]:
        s = self.sol
        out = [f"# 8. 改修時の注意点 — {s['file_name']}", "",
               "機械的に拾えるリスク箇所を並べてある。"
               "**値そのものは載せていない**ので、確認は該当ファイルを直接見ること。",
               "", "## 認証情報が埋め込まれている可能性がある箇所", ""]
        rows = []
        for label, pat in RISK_SIGNALS:
            hits = self.scripts_matching(pat)
            if hits:
                rows.append([label, len(hits), _names(hits, 10)])
        if rows:
            out += _table(["検出内容", "件数", "スクリプト"], rows)
            out += ["> DDR に平文で残っている認証情報は、DDR を配る前に伏せる。"
                    "実システム側の資格情報も変更を検討する。", ""]
        else:
            out += ["（検出なし）", ""]

        long_scripts = sorted(s["scripts"], key=lambda x: -x["steps"])[:10]
        many_disabled = sorted((x for x in s["scripts"] if x["disabled"]),
                               key=lambda x: -x["disabled"])[:10]
        out += ["## 長大なスクリプト", ""]
        out += _table(["スクリプト", "ステップ", "無効ステップ"],
                      [[f"[{x['name']}]({self.link(x['path'])})", x["steps"],
                        x["disabled"]] for x in long_scripts])
        out += ["## 無効化ステップが多いスクリプト（過去の改修跡）", ""]
        out += _table(["スクリプト", "無効ステップ", "全ステップ"],
                      [[x["name"], x["disabled"], x["steps"]] for x in many_disabled]) \
            if many_disabled else ["（無効化ステップはありません）", ""]

        out += ["## 同名の要素（どちらを直すか間違えやすい）", ""]
        dup_lines = []
        for key, label in (("scripts", "スクリプト"), ("layouts", "レイアウト"),
                           ("valuelists", "値一覧")):
            dups = self.dup_names(key)
            if dups:
                dup_lines.append(f"- {label}: " +
                                 ", ".join(f"{n}（{c} 件）" for n, c in dups[:20]))
        out += (dup_lines or ["（同名の要素はありません）"]) + [""]

        legacy = {
            "スクリプト": [x["name"] for x in s["scripts"] if _has_legacy(x["name"])],
            "レイアウト": [x["name"] for x in s["layouts"] if _has_legacy(x["name"])],
            "テーブル": [x["name"] for x in s["base_tables"] if _has_legacy(x["name"])],
            "TO": [x["name"] for x in s["table_occurrences"] if _has_legacy(x["name"])],
        }
        out += ["## 旧版・作業途中らしい要素（名前からの推測）", ""]
        out += _table(["種別", "件数", "名前"],
                      [[k, len(v), _names(v, 15)] for k, v in legacy.items() if v])
        out += ["> 「旧」「コピー」「テスト」などの語で拾っているだけ。"
                "実際に使われているかは 9 章の棚卸しと合わせて判断する。", ""]

        out += _todo("触るときの手順",
                     "改修の進め方（バックアップ、検証用ファイルの作り方、"
                     "本番反映の手順、戻し方）を書く。"
                     "壊すと影響が大きい箇所（連鎖削除・共通スクリプト・"
                     "完全アクセス権で実行するスクリプト）を名指しで挙げる。",
                     [self.link(f"{s['dir']}/Relationships.md")])
        out += _todo("既知の不具合・未完了の作業",
                     "スクリプトコメントに残っている TODO や"
                     "「未完成」「リリースまでコメントアウト」のような記述を拾って整理する。",
                     ["grep -rn 'TODO\\|未完成\\|暫定\\|要確認' " +
                      self.link(f"{s['dir']}/Scripts/")])
        return out

    def ch_audit(self) -> list[str]:
        s, a = self.sol, self.audit
        out = [f"# 9. 棚卸し — {s['file_name']}", "",
               "使われていない可能性がある要素の一覧。**候補であって断定ではない。**",
               "メニューからの手動実行、レイアウトメニューでの切り替え、"
               "スクリプト名やフィールド名を計算で組み立てる参照、"
               "外部ファイルからの参照は検出できない。", ""]
        out += ["チェックボックスは「消してよい」と判断できたものに付ける。", ""]
        for key, label, total_key, note in AUDIT_SECTIONS:
            items = a[key]
            total = a["totals"][total_key]
            out += [f"## {label}  {len(items)} / {total}", ""]
            if items:
                out += [f"- [ ] {n}" for n in items[:self.opts.limit]]
                if len(items) > self.opts.limit:
                    out.append(f"- …他 {len(items) - self.opts.limit} 件"
                               f"（全件は `fmddr.py audit -d {self.rel}"
                               f" --limit 9999` で出る）")
            else:
                out.append("（該当なし）")
            out.append("")
            if note:
                out += [f"> {note}", ""]
        out += _todo("削除可否の判断",
                     "上の候補それぞれについて「消してよい / 残す / 要調査」を付ける。"
                     "残すものは理由（手動実行する、外部ファイルから呼ばれる 等）を書く。",
                     [f"fmddr.py xref -d {self.rel} --script \"<名前>\""])
        out += _todo("技術的負債の整理",
                     "旧版の残骸、重複、命名の不統一、"
                     "同じ処理が複数箇所にコピーされている箇所を挙げ、"
                     "手を付ける優先順位を付ける。")
        return out

    def ch_client(self) -> list[str]:
        s = self.sol
        groups: dict[str, list] = {}
        for lay in s["layouts"]:
            groups.setdefault(lay["group"] or "（グループなし）", []).append(lay["name"])
        forms = [l["name"] for l in s["layouts"]
                 if any(x.casefold() in l["name"].casefold() for x in FORM_WORDS)]
        custom_vl = [v["name"] for v in s["valuelists"] if v["kind"] == "Custom"]

        out = [f"# 10. 業務仕様サマリ — {s['file_name']}", "",
               "顧客・発注元向けの章。**内部実装の詳細（スクリプト名・"
               "フィールドの計算式・アカウント）は書かない。**", "",
               "## 画面構成", "",
               "「掲載」が要検討の区分は、メンテナンス用・旧版・内部連携用の"
               "レイアウトグループ。顧客向けには載せないのが普通なので、"
               "残すかどうかを判断してから書く。", ""]
        internal = ("メンテ", "not_use", "not use", "Tableau", "作業", "旧", "開発")
        out += _table(["区分", "画面数", "掲載", "画面"],
                      [[g, len(v),
                        "要検討" if any(w.casefold() in g.casefold()
                                        for w in internal) else "-",
                        _names(sorted(v), 40)]
                       for g, v in sorted(groups.items())])
        out += ["## 帳票", "",
                f"- {len(forms)} 件: {_names(sorted(forms), 60)}", "",
                "## 管理するデータ", ""]
        out += _table(["データ", "項目数", "主な項目"],
                      [[b["name"], b["fields"], _names(b["field_names"][:12], 12)]
                       for b in sorted(s["base_tables"], key=lambda x: -x["fields"])],
                      limit=40)
        out += ["## 選択肢マスタ（値一覧）", "",
                f"- 固定値の値一覧 {len(custom_vl)} 件: {_names(sorted(custom_vl), 60)}",
                f"- 中身は [ValueLists.md]({self.link(s['dir'] + '/ValueLists.md')}) を参照", ""]
        out += _todo("業務フロー",
                     "業務の流れを、画面と帳票の名前だけを使って説明する。"
                     "内部のテーブル名・スクリプト名は出さない。")
        out += _todo("用語集",
                     "画面やデータ項目に出てくる社内用語・略語を、"
                     "この資料を初めて読む人向けに定義する。")
        return out


    def ch_er(self) -> list[str]:
        s = self.sol
        pairs = self.er_pairs()
        islands = self.er_islands()

        def edges(scope=None, touching=None):
            """scope: 両端がその集合に入る線だけ / touching: その表に接する線だけ"""
            out = []
            for (a, b), v in sorted(pairs.items()):
                if scope and (a not in scope or b not in scope):
                    continue
                if touching and touching not in (a, b):
                    continue
                sym = "||--o{" if v["card"] == "1対多" else "}o--o{"
                out.append(f'  {mm_id(a)} {sym} {mm_id(b)} : "{v["n"]}本"')
            return out

        out = [f"# 11. ER 図 — {s['file_name']}", "",
               f"- BaseTable {s['counts']['base_tables']} / "
               f"TO {s['counts']['table_occurrences']} / "
               f"リレーション {s['counts']['relationships']}",
               "- **TO はベーステーブル単位に集約**している"
               "（TO のままでは 300 超のノードになって読めないため）",
               "- 述語ごとの定義は "
               f"[Relationships.md]({self.link(s['dir'] + '/Relationships.md')}) にもある", "",
               "> ⚠️ **多重度は結合フィールド名からの推定。** FileMaker は基数を宣言しないので、"
               "関連線は「結合が定義されている」ことを示すだけで、1 対多の保証はない。", "",
               "## 全体図", ""]
        out += ["```mermaid", "erDiagram"] + edges() + ["```", ""]

        multi = [g for g in islands if len(g) > 1]
        alone = [g[0] for g in islands if len(g) == 1]
        if len(multi) > 1:
            out += ["## 島ごとの図", "",
                    f"リレーショングラフは {len(multi)} 個の島に分かれている。", ""]
            for i, g in enumerate(multi, 1):
                out += [f"### 島 {i}（{len(g)} テーブル）", "",
                        f"- {_names(g, 30)}", "",
                        "```mermaid", "erDiagram"] + edges(set(g)) + ["```", ""]

        # 1 つの島に繋がっていると全体図が読めないので、ハブごとに 1 ホップで切り出す
        deg: dict[str, int] = {}
        for a, b in pairs:
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        if multi and len(multi[0]) > 20:
            hubs = sorted(deg.items(), key=lambda x: -x[1])[:8]
            out += ["## 主要テーブル周辺図", "",
                    f"最大の島が {len(multi[0])} テーブルあり全体図では追えないので、"
                    "関連の多いテーブルごとに**そのテーブルに接する線だけ**を切り出す。", ""]
            for name, d in hubs:
                out += [f"### {name}（関連 {d}）", "",
                        "```mermaid", "erDiagram"] + edges(touching=name) + ["```", ""]

        if alone:
            out += ["## リレーションを持たないテーブル", "",
                    f"- {len(alone)} 件: {_names(alone, 40)}", "",
                    "> スクリプトから直接読み書きしているだけの作業用テーブル、"
                    "または使われていない残骸。9 章の棚卸しと突き合わせる。", ""]

        out += ["## テーブル一覧", ""]
        rows = []
        for b in sorted(s["base_tables"], key=lambda x: -x["fields"]):
            occ = self.xref.get("base_tables", {}).get(b["name"], {}).get("occurrences") or []
            keys = [f for f in b["field_names"] if KEYISH.search(f)][:6]
            deg = sum(1 for (x, y) in pairs if b["name"] in (x, y))
            rows.append([f"[{b['name']}]({self.link(b['path'])})", mm_id(b["name"]),
                         b["fields"], b["records"], len(occ), deg,
                         _names(keys, 6)])
        out += _table(["テーブル", "図中の表記", "フィールド", "レコード", "TO",
                       "関連", "キー候補"], rows)

        out += ["## リレーション定義（全件）", ""]
        rows = []
        for r in s["relationships"]:
            casc = []
            for side, label in (("left", "左"), ("right", "右")):
                f = [{"cascadeCreate": "作成", "cascadeDelete": "削除"}[k]
                     for k in r["cascade"][side]]
                if f:
                    casc.append(f"{label}:{'/'.join(f)}")
            rows.append([r["left"], r["right"], infer_cardinality(r["predicates"]),
                         " / ".join(f"{p['left']} {p['type']} {p['right']}"
                                    for p in r["predicates"]) or "-",
                         " ".join(casc) or "-"])
        out += _table(["左 TO", "右 TO", "多重度（推定）", "結合条件", "連鎖"], rows)
        out += _todo("テーブルの役割と島の意味",
                     "島ごとに何の業務を担っているか、島をまたぐ参照がどこで起きているかを"
                     "書く。多対多と出ている関連のうち、実際は 1 対多のものを直す。",
                     [self.link(f"{s['dir']}/Relationships.md"),
                      self.link(f"{s['dir']}/BaseTables/")])
        return out

    def ch_flow(self) -> list[str]:
        s = self.sol
        trs = self.transitions()
        groups: dict[str, list] = {}
        for lay in s["layouts"]:
            groups.setdefault(lay["group"] or "（グループなし）", []).append(lay["name"])

        # 帳票・印刷レイアウトへの切り替えは「出力のための一時的な移動」なので
        # 遷移図から外す（外さないと図が帳票で埋まる）。一覧には残す。
        forms = {l["name"] for l in s["layouts"]
                 if any(w.casefold() in l["name"].casefold() for w in FORM_WORDS)}

        # 図では (起点, 遷移先) で 1 本に束ねる。経由スクリプトはラベルにまとめる
        merged: dict[tuple[str, str], dict] = {}
        for t in trs:
            if t["dst"] in forms:
                continue
            slot = merged.setdefault((t["src"], t["dst"]),
                                     {"direct": False, "vias": []})
            if t["kind"] == "direct":
                slot["direct"] = True
            elif t["via"]:
                slot["vias"].append(t["via"])

        out = [f"# 12. 画面遷移図 — {s['file_name']}", "",
               f"- レイアウト {s['counts']['layouts']} / 抽出した遷移 {len(trs)} 本"
               f"（図に描くのは帳票を除いた {len(merged)} 経路）",
               "- 実線 `-->` … レイアウト上のボタン・トリガが直接切り替える",
               "- 破線 `-.->` … レイアウトが呼ぶスクリプトの中で切り替える"
               "（ラベルは経由スクリプト）",
               "- **レイアウトグループごとに図を分け、同じ経路は 1 本に束ねている**"
               "（214 画面・871 経路を 1 枚にすると読めない）",
               "- **帳票・印刷レイアウトへの切り替えは図から外している。**"
               "出力のための一時的な移動で、画面遷移ではないため（下の「帳票出力」節に分けた）", "",
               "> ⚠️ **網羅性は保証できない。** FileMaker はスクリプトから任意のレイアウトへ"
               "飛べるうえ、レイアウト名を計算で組み立てる切り替えは追えない。"
               "逆に、スクリプトが内部で一時的に切り替えるだけの経路も拾ってしまう。"
               "最終確認は現物の操作とすり合わせること。", ""]

        for g, names in sorted(groups.items()):
            inside = {k: v for k, v in merged.items() if k[0] in names}
            if not inside:
                continue
            nodes = sorted({a for a, _ in inside} | {b for _, b in inside})
            ids = {n: f"N{i}" for i, n in enumerate(nodes)}
            out += [f"## {g}（画面 {len(names)} / 経路 {len(inside)}）", "",
                    "```mermaid", "flowchart LR"]
            for n in nodes:
                mark = "" if n in names else " ※他グループ"
                out.append(f'  {ids[n]}["{mm_label(n)}{mark}"]')
            for (src, dst), v in sorted(inside.items()):
                if v["direct"]:
                    out.append(f"  {ids[src]} --> {ids[dst]}")
                else:
                    vias = sorted(set(v["vias"]))
                    lab = mm_label(vias[0]) + (f" +{len(vias) - 1}" if len(vias) > 1 else "")
                    out.append(f"  {ids[src]} -.->|{lab}| {ids[dst]}")
            out += ["```", ""]

        out += ["## 帳票出力（どの画面からどの帳票を出すか）", ""]
        form_rows = [[t["src"], t["via"] or "（直接）", t["dst"]]
                     for t in trs if t["dst"] in forms]
        out += _table(["起点画面", "経由スクリプト", "帳票レイアウト"], form_rows) \
            if form_rows else ["（検出なし）", ""]

        out += ["## 遷移一覧（全件）", "",
                "図から外した帳票への切り替えも含めた全件。", ""]
        out += _table(["起点画面", "種別", "経由スクリプト", "遷移先画面"],
                      [[t["src"], "直接" if t["kind"] == "direct" else "処理経由",
                        t["via"] or "-", t["dst"]] for t in trs])

        no_out = [l["name"] for l in s["layouts"]
                  if not any(t["src"] == l["name"] for t in trs)]
        no_in = [l["name"] for l in s["layouts"]
                 if not any(t["dst"] == l["name"] for t in trs)]
        out += ["## 行き先・入り口が検出できなかった画面", "",
                f"- 出口なし {len(no_out)} 件: {_names(no_out, 40)}",
                f"- 入口なし {len(no_in)} 件: {_names(no_in, 40)}", "",
                "> 印刷用レイアウトはスクリプトが切り替えるだけでボタンを持たないので"
                "「出口なし」に出る。9 章の未使用候補と混同しないこと。", ""]
        out += _todo("画面遷移の説明",
                     "業務の流れに沿って、どの画面から入ってどこへ抜けるかを書く。"
                     "図に出ていない遷移（メニューからの切り替え、計算で組み立てる遷移）が"
                     "あれば補う。",
                     [self.link(f"{s['dir']}/Layouts/")])
        return out

    def ch_functions(self) -> list[str]:
        s = self.sol
        feats: dict[str, list[str]] = {}
        per_script: dict[str, list[str]] = {}
        for sc in s["scripts"]:
            fs = step_features(self.body_of(sc))
            per_script[sc["path"]] = fs
            for f in fs:
                feats.setdefault(f, []).append(sc["name"])

        out = [f"# 13. 機能一覧 — {s['file_name']}", "",
               f"- スクリプト {s['counts']['scripts']}",
               "- **実処理** はコメント・空行を除いたステップ数。ロジック量の目安"
               "（DDR のステップ総数はコメントを含むので 1.5〜2 倍に見える）",
               "- **固有機能** は FileMaker 固有で、他基盤へ移すときに代替実装の検討が"
               "必要になる要素", "", "## 固有機能の使用状況", ""]
        out += _table(["固有機能", "使用スクリプト数", "スクリプト"],
                      [[k, len(v), _names(sorted(v), 10)]
                       for k, v in sorted(feats.items(), key=lambda x: -len(x[1]))])

        by_group: dict[str, list] = {}
        for sc in s["scripts"]:
            by_group.setdefault(sc["group"] or "（グループなし）", []).append(sc)
        out += ["## スクリプト一覧（グループ別）", ""]
        for g, items in sorted(by_group.items()):
            out += [f"### {g}（{len(items)} 件）", ""]
            rows = []
            for i in sorted(items, key=lambda x: -x["steps"]):
                called_by = sorted(self.script_xref(i["name"]).get("called_by") or [])
                rows.append([f"[{i['name']}]({self.link(i['path'])})",
                             real_steps(self.body_of(i)), i["steps"], i["disabled"],
                             _names(sorted(i["refs"].get("scripts") or []), 5),
                             _names(sorted(i["refs"].get("layouts") or []), 5),
                             _names(per_script.get(i["path"]) or [], 6),
                             len(called_by)])
            out += _table(["スクリプト", "実処理", "全ステップ", "無効",
                           "呼び出すスクリプト", "参照レイアウト", "固有機能",
                           "呼び出し元数"], rows)
        out += _todo("機能の業務単位への束ね",
                     "スクリプト名の羅列ではなく「業務としての機能」に束ねた一覧を作る。"
                     "4 章の入口と呼び出しツリーを起点に、"
                     "1 機能 = 1 入口 + 配下のサブスクリプト群として数える。",
                     [self.link(f"{s['dir']}/Scripts/")])
        return out


    # --- 14 章 / 15 章の素材 -------------------------------------------------

    def field_defs(self):
        """(テーブル, フィールド定義) を全件返す。"""
        return [(b["name"], f) for b in self.sol["base_tables"]
                for f in b["field_defs"]]

    def rules_by_kind(self) -> dict[str, list[tuple[str, dict, str]]]:
        """details を種別ごとに束ねる。値は (テーブル, フィールド, 本文)。"""
        kinds: dict[str, list] = {}
        for tbl, f in self.field_defs():
            for d in f["details"]:
                head = d.split(":", 1)[0].split(" (")[0].strip()
                kinds.setdefault(head, []).append((tbl, f, d))
        return kinds

    def ch_rules(self) -> list[str]:
        s = self.sol
        defs = self.field_defs()
        kinds = self.rules_by_kind()
        ruled = [(t, f) for t, f in defs if f["details"]]

        out = [f"# 14. ビジネスルール台帳 — {s['file_name']}", "",
               f"- フィールド {s['counts']['fields']} のうち "
               f"**{len(ruled)} 件が計算式・自動入力・入力値の制限を持つ**",
               "- 「この値はどう決まるのか」をフィールド定義側から引くための台帳。"
               "スクリプト側に書かれたルールは 4 章と 13 章を見る",
               "- 全文は各テーブルの Markdown"
               f"（[BaseTables/]({self.link(s['dir'] + '/BaseTables/')})）にもある", "",
               "## ルールの種別と件数", ""]
        labels = {
            "計算式": "計算フィールド（保存されず毎回計算、または保存される計算列）",
            "自動入力 計算値": "自動入力の計算値（入力時に 1 度だけ、または常に評価）",
            "ルックアップ": "ルックアップ（リレーション経由で値をコピー）",
            "自動入力 作成/修正情報": "作成者・作成日時・修正者・修正日時の自動記録",
            "入力値の制限": "入力値の制限（空欄不可・ユニーク・値一覧・計算による検証）",
            "自動入力 データ": "固定値の自動入力",
            "入力値の変更を許さない": "自動入力後の手修正を禁止",
            "集計": "集計フィールド（合計・平均・件数など）",
            "シリアル番号": "シリアル番号（採番）",
        }
        out += _table(["種別", "件数", "意味"],
                      [[k, len(v), labels.get(k, "-")]
                       for k, v in sorted(kinds.items(), key=lambda x: -len(x[1]))])

        per_table: dict[str, int] = {}
        for t, f in ruled:
            per_table[t] = per_table.get(t, 0) + len(f["details"])
        out += ["## テーブル別のルール数", ""]
        out += _table(["テーブル", "ルール数", "フィールド数"],
                      [[t, n, next(b["fields"] for b in s["base_tables"]
                                   if b["name"] == t)]
                       for t, n in sorted(per_table.items(), key=lambda x: -x[1])])

        # 計算フィールドは式そのものが仕様なので全文を載せる
        calc = [(t, f) for t, f in defs if f["fieldType"] == "Calculated"]
        out += [f"## 計算フィールド（{len(calc)} 件）", "",
                "式そのものが仕様なので全文を載せる。"
                "**非保存**は参照のたびに計算されるので、移行時は性能設計に影響する。", ""]
        for t, f in sorted(calc, key=lambda x: (x[0], x[1]["name"])):
            flags = f"（{', '.join(f['flags'])}）" if f["flags"] else ""
            out += [f"### {t}::{f['name']}  {f['dataType']}{flags}", ""]
            if f["comment"]:
                out.append(f"- コメント: {one_line(f['comment'])}")
            if f["refs"].get("fields"):
                out.append(f"- 参照フィールド: {_names(sorted(f['refs']['fields']), 20)}")
            body = next((d for d in f["details"] if d.startswith("計算式")), "")
            out += fence(body.split("\n", 1)[1] if "\n" in body else "（式なし）",
                         "filemaker") + [""]

        for kind, title, note in (
                ("自動入力 計算値", "自動入力の計算値",
                 "入力時に値を作るルール。式は 1 行に丸めてある"),
                ("ルックアップ", "ルックアップ",
                 "リレーション経由で値をコピーする。コピー元が変わっても遡って直らない"),
                ("入力値の制限", "入力値の制限",
                 "画面から入る値の制約。移行先ではアプリ側の検証に置き換える必要がある"),
                ("シリアル番号", "シリアル番号（採番）",
                 "移行時は次の値を引き継がないと採番が衝突する"),
                ("集計", "集計フィールド",
                 "移行先では集計クエリに置き換える。カラムにしない"),
                ("自動入力 データ", "固定値の自動入力", ""),
        ):
            items = kinds.get(kind) or []
            out += [f"## {title}（{len(items)} 件）", ""]
            if not items:
                out += ["（該当なし）", ""]
                continue
            if note:
                out += [f"{note}。", ""]
            out += _table(["テーブル", "フィールド", "型", "内容", "参照フィールド"],
                          [[t, f["name"], f["dataType"],
                            one_line(d.split(":", 1)[1].strip() if ":" in d else d, 120),
                            _names(sorted(f["refs"].get("fields") or []), 6)]
                           for t, f, d in sorted(items, key=lambda x: (x[0], x[1]["name"]))])

        out += ["## 移行時に手当てが必要なフィールド", ""]
        groups = [
            ("非保存の計算", "参照のたびに計算される。移行先ではクエリかアプリ側で"
             "都度計算する。性能設計に影響する",
             lambda f: "非保存" in f["flags"]),
            ("グローバル", "全レコード共通の一時値。**カラムにしない。**"
             "セッション変数や設定テーブルへ移す",
             lambda f: "グローバル" in f["flags"]),
            ("繰り返しフィールド", "1 フィールドに複数値が入っている。"
             "**子テーブルへ正規化が必要**",
             lambda f: any(x.startswith("繰り返し") for x in f["flags"])),
        ]
        for label, note, pred in groups:
            hit: dict[str, list[str]] = {}
            for t, f in defs:
                if pred(f):
                    hit.setdefault(t, []).append(f["name"])
            total = sum(len(v) for v in hit.values())
            out += [f"### {label}（{total} 件 / {len(hit)} テーブル）", "",
                    f"{note}。", ""]
            out += _table(["テーブル", "件数", "フィールド"],
                          [[t, len(v), _names(sorted(v), 25)]
                           for t, v in sorted(hit.items(), key=lambda x: -len(x[1]))]) \
                if hit else ["（該当なし）", ""]
            out += ["> 件数は全件。名前は 25 件までを載せている"
                    "（全件は各テーブルの Markdown を見る）。", ""]

        out += _todo("ルールの業務的な意味",
                     "上の計算フィールドと入力値の制限のうち、業務判断が入っているものに"
                     "「なぜそうなっているか」を書く。単なる転記や表示整形は"
                     "「機械的」と書いて飛ばしてよい。数が多いので、"
                     "金額・日付・区分に関わるものから手を付ける。",
                     [self.link(f"{s['dir']}/BaseTables/")])
        out += _todo("同じルールの二重管理",
                     "同じ計算がフィールド定義とスクリプトの両方に書かれている箇所を挙げる。"
                     "改修時に片方だけ直して事故になる典型なので、名指しで残す。",
                     [f"fmddr.py xref -d {self.rel} --field \"<テーブル::フィールド>\""])
        return out

    def ch_naming(self) -> list[str]:
        s = self.sol
        defs = self.field_defs()

        def prefixes(names, min_count=3):
            c: dict[str, int] = {}
            for n in names:
                if "_" in n[1:]:
                    c[n.split("_")[0] + "_"] = c.get(n.split("_")[0] + "_", 0) + 1
            return sorted(((k, v) for k, v in c.items() if v >= min_count),
                          key=lambda x: -x[1])

        out = [f"# 15. 命名規則の実態 — {s['file_name']}", "",
               "**規約として決まっているものではなく、実際の名前を数えた結果。**",
               "後任が最初に知りたい「この名前は何を意味するのか」を、"
               "使用率とともに示す。例外の数も出るので、規約として採用できるかが判断できる。", "",
               "## 接頭辞の実態", ""]
        for label, names in (
                ("テーブル", [b["name"] for b in s["base_tables"]]),
                ("テーブルオカレンス", [t["name"] for t in s["table_occurrences"]]),
                ("スクリプト", [x["name"] for x in s["scripts"]]),
                ("レイアウト", [x["name"] for x in s["layouts"]]),
                ("フィールド", [f["name"] for _, f in defs]),
        ):
            pre = prefixes(names)
            out += [f"### {label}（{len(names)} 件）", ""]
            out += _table(["接頭辞", "件数", "割合"],
                          [[p, n, f"{n / max(1, len(names)) * 100:.1f}%"]
                           for p, n in pre], limit=15) if pre else \
                ["（`_` 区切りの接頭辞は検出されませんでした）", ""]

        out += ["## テーブル名の接尾辞", ""]
        suf: dict[str, int] = {}
        for b in s["base_tables"]:
            for w in ("DB", "Master", "マスタ", "履歴", "ログ", "設定", "情報", "一覧"):
                if b["name"].endswith(w):
                    suf[w] = suf.get(w, 0) + 1
                    break
        out += _table(["接尾辞", "件数"], [[k, v] for k, v in
                                          sorted(suf.items(), key=lambda x: -x[1])]) \
            if suf else ["（該当なし）", ""]

        # 命名と実際の属性が食い違っている箇所（規約違反の検出）
        out += ["## 命名と実態の食い違い", "",
                "接頭辞・接尾辞から期待される属性と、実際のフィールド設定を突き合わせた。", ""]
        gname = {f"{t}::{f['name']}" for t, f in defs
                 if re.search(r"(^|_)g(_|$)", f["name"], re.I)}
        gflag = {f"{t}::{f['name']}" for t, f in defs if "グローバル" in f["flags"]}
        out += _table(["食い違い", "件数"],
                      [["名前に g が入っているのにグローバルでない", len(gname - gflag)],
                       ["グローバルなのに名前に g が入っていない", len(gflag - gname)]])
        out += [f"> グローバルフィールドは全 {len(gflag)} 件、"
                f"名前に `g` を含むものは {len(gname)} 件。"
                "**この差の大きさが、`g` という命名が規約として機能していないことを示している。**"
                "移行時はフィールド名ではなく Storage の属性で判定すること。", ""]
        for label, keys in (("名前に g が入っているのにグローバルでない", gname - gflag),
                            ("グローバルなのに名前に g が入っていない", gflag - gname)):
            grouped: dict[str, list[str]] = {}
            for k in sorted(keys):
                t, _, n = k.partition("::")
                grouped.setdefault(t, []).append(n)
            out += [f"#### {label}（{len(keys)} 件）", ""]
            out += _table(["テーブル", "件数", "フィールド"],
                          [[t, len(v), _names(v, 25)]
                           for t, v in sorted(grouped.items(), key=lambda x: -len(x[1]))]) \
                if grouped else ["（該当なし）", ""]

        # セパレータ用のダミーフィールド
        seps = [f"{t}::{f['name']}" for t, f in defs
                if re.fullmatch(r"[-_=＝ー─\s]{3,}", f["name"] or "")]
        out += ["## 区切り用のダミーフィールド", "",
                f"- {len(seps)} 件: {_names(sorted(seps), 20)}", "",
                "> フィールド一覧を見やすくするための飾り。移行対象にしない。", ""]

        # 表記ゆれ（正規化すると衝突する名前）
        out += ["## 表記ゆれの候補", "",
                "全角/半角・大文字小文字・空白を無視すると同じになる名前。"
                "どちらが正なのか分からなくなる原因になる。", ""]
        import unicodedata
        for label, names in (("スクリプト", [x["name"] for x in s["scripts"]]),
                             ("レイアウト", [x["name"] for x in s["layouts"]]),
                             ("フィールド", sorted({f["name"] for _, f in defs}))):
            norm: dict[str, set] = {}
            for n in names:
                k = unicodedata.normalize("NFKC", n).lower().replace(" ", "")
                norm.setdefault(k, set()).add(n)
            clash = [sorted(v) for v in norm.values() if len(v) > 1]
            out += [f"- {label}: {len(clash)} 組" +
                    ("" if not clash else " — " +
                     "; ".join(" / ".join(g) for g in clash[:8]))]
        out += [""]

        # 連番・派生コピーの命名
        dup_pat = re.compile(r"(コピー|copy| \d+$|２|_2$|旧|old|新|New)", re.I)
        rows = []
        for label, names in (("スクリプト", [x["name"] for x in s["scripts"]]),
                             ("レイアウト", [x["name"] for x in s["layouts"]]),
                             ("テーブルオカレンス", [t["name"] for t in s["table_occurrences"]])):
            hit = [n for n in names if dup_pat.search(n)]
            rows.append([label, len(hit), f"{len(hit) * 100 // max(1, len(names))}%",
                         _names(sorted(hit), 12)])
        out += ["## 派生・世代を名前で表しているもの", ""]
        out += _table(["種別", "件数", "割合", "名前"], rows)
        out += ["> 「旧」「New」「コピー」で世代を管理している。"
                "どれが現行かは名前では決まらないので、8 章・9 章と併せて判断する。", ""]

        out += _todo("規約として採用するもの",
                     "上の実態のうち「今後も守るもの」と「例外として残すだけのもの」を"
                     "分けて書く。守るものは接頭辞・接尾辞の意味を定義する。"
                     "食い違いが多いものは規約にしない（守られないため）。")
        out += _todo("命名の落とし穴",
                     "後任が引っかかる名前を挙げる。同名の別要素、"
                     "似ているが意味が違う名前、名前と実態が違うフィールドなど。",
                     [f"{self.link(s['dir'])}/BaseTables/"])
        return out


CHAPTERS = [
    ("01_システム概要.md", ("dev", "survey", "client"), "ch_overview"),
    ("02_テーブル構成.md", ("dev",), "ch_tables"),
    ("03_リレーション.md", ("dev",), "ch_relations"),
    ("04_主要処理フロー.md", ("dev",), "ch_flows"),
    ("05_画面一覧.md", ("dev", "client"), "ch_layouts"),
    ("06_外部連携.md", ("dev", "client"), "ch_external"),
    ("07_権限マトリクス.md", ("dev",), "ch_security"),
    ("08_改修時の注意点.md", ("dev",), "ch_risks"),
    ("09_棚卸し.md", ("survey",), "ch_audit"),
    ("10_業務仕様サマリ.md", ("client",), "ch_client"),
    ("11_ER図.md", ("dev", "survey"), "ch_er"),
    ("12_画面遷移図.md", ("dev", "client"), "ch_flow"),
    ("13_機能一覧.md", ("dev", "survey"), "ch_functions"),
    ("14_ビジネスルール台帳.md", ("dev", "survey"), "ch_rules"),
    ("15_命名規則の実態.md", ("dev", "survey"), "ch_naming"),
]

AUDIENCES = {"dev": "後任の開発者向け（開発引き継ぎ）",
             "survey": "現状調査レポート（棚卸し）",
             "client": "顧客・発注元向けの仕様書"}


def cmd_handover(args):
    index, xrefs = load_index(args.dir)
    split_root = os.path.abspath(args.dir)
    out = os.path.abspath(args.out)
    if out == split_root:
        raise SystemExit("-o は split の出力先と別のディレクトリにしてください。")

    picked = ["dev", "survey", "client"] if args.audience == "all" else \
        [a.strip() for a in args.audience.split(",") if a.strip()]
    unknown = [a for a in picked if a not in AUDIENCES]
    if unknown:
        raise SystemExit(f"--audience の指定が不正です: {', '.join(unknown)}"
                         f"（使えるのは all / {' / '.join(AUDIENCES)}）")

    prev = []
    marker = os.path.join(out, MANIFEST)
    if os.path.exists(marker):
        try:
            prev = json.load(open(marker, encoding="utf-8")).get("files", [])
        except (json.JSONDecodeError, OSError):
            prev = []
    os.makedirs(out, exist_ok=True)
    w = Writer(out)

    multi = len(index["solutions"]) > 1
    sol_links = []
    for sol in index["solutions"]:
        prefix = f"{sol['dir']}/" if multi else ""
        ho = Handover(sol, xrefs.get(sol["dir"], {}), split_root,
                      os.path.join(out, sol["dir"]) if multi else out, args)
        written = []
        for fname, auds, method in CHAPTERS:
            if not set(auds) & set(picked):
                continue
            lines = getattr(ho, method)()
            lines += ["---", "",
                      f"<sub>fmddr {VERSION} が生成した骨組みに加筆した資料。"
                      f"元データ: `{sol['file_name']}` の DDR "
                      f"（FileMaker {sol['ddr'].get('version')} / "
                      f"{sol['ddr'].get('creationDate')}）</sub>"]
            written.append((fname, w.write(prefix + fname, lines)))
        sol_links.append((sol, prefix, written))

    # --- 全体の README（目次と執筆手順）
    todo_count = 0
    for _, _, written in sol_links:
        for _, rel in written:
            with open(os.path.join(out, rel), encoding="utf-8") as fh:
                todo_count += sum(1 for ln in fh if TODO_MARK in ln and ln.startswith(">"))

    lines = ["# 引き継ぎ資料", "",
             f"- 対象: " + ", ".join(s["file_name"] for s, _, _ in sol_links),
             f"- 読者: " + ", ".join(AUDIENCES[a] for a in picked),
             f"- 骨組み生成: fmddr {VERSION} / "
             f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"- 解析元: `{index['input']}`", "", "## 目次", ""]
    for sol, prefix, written in sol_links:
        if multi:
            lines += [f"### {sol['file_name']}", ""]
        for fname, rel in written:
            title = fname.split("_", 1)[1].removesuffix(".md")
            auds = next(a for f, a, _ in CHAPTERS if f == fname)
            lines.append(f"- [{title}]({rel})  — {' / '.join(auds)}")
        lines.append("")

    lines += ["## この資料の作り方", "",
              "各章は **事実パート（機械生成）** と **執筆枠** でできている。"
              f"執筆枠は `{TODO_MARK}` で始まる引用ブロックで、"
              f"いま **{todo_count} 箇所** 残っている。", "",
              "1. 事実パートはそのまま使う（件数・一覧・依存関係は DDR から生成済み）",
              f"2. `grep -rn '{TODO_MARK}' .` で残りを確認する",
              "3. 各枠の「手掛かり」に挙がっている split 出力を読んで、枠を文章に置き換える",
              "4. 置き換えたら引用ブロックごと消す（マーカーが残っていると未完了扱い）",
              f"5. 最後に `grep -rc '{TODO_MARK}' .` が 0 になったことを確認する", "",
              "DDR を再出力したら、同じコマンドで骨組みを作り直せる。"
              "**加筆した本文は上書きされる**ので、書き終えた資料は別ディレクトリか"
              "Git のコミットで残しておく。", "",
              "## 注意", "",
              "- スクリプトのコメント・アカウント一覧・値一覧には"
              "実在の顧客名・担当者名が含まれることがある。配布範囲を決めてから書く",
              "- 8 章は認証情報が埋め込まれている箇所を指しているだけで、"
              "値そのものは載せていない。**値を資料に転記しないこと**",
              "- 「未使用」「帳票らしい」などの推測は命名やステップ名に依存する。"
              "断定する前に該当ファイルを確認する"]
    w.write("README.md", lines)

    manifest = {"fmddr": VERSION, "kind": "handover",
                "generated": datetime.now().isoformat(timespec="seconds"),
                "split": split_root, "audience": picked,
                "files": sorted(w.written)}
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    removed = 0
    if args.clean and prev:
        keep = set(w.written)
        for rel in prev:
            full = os.path.join(out, rel)
            if rel not in keep and os.path.isfile(full):
                os.remove(full)
                removed += 1

    print(f"出力先: {out}")
    print(f"生成ファイル: {len(w.written)}" + (f" / 削除: {removed}" if removed else ""))
    print(f"執筆枠（{TODO_MARK}）: {todo_count} 箇所")
    print("次にやること: README.md を読み、各章の執筆枠を埋める")


# ---------------------------------------------------------------------------
# 9. エントリポイント
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="fmddr",
        description="FileMaker DDR を AI が読める形に分解する（標準ライブラリのみ）")
    p.add_argument("--version", action="version", version=f"fmddr {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="DDR の種別・件数・構成ファイルを表示")
    sp.add_argument("-i", "--input", required=True)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("split", help="DDR を Markdown + JSON 索引に分解")
    sp.add_argument("-i", "--input", required=True, help="概要ファイル または *_fmp12.xml")
    sp.add_argument("-o", "--out", required=True, help="出力先ディレクトリ")
    sp.add_argument("--clean", action="store_true",
                    help="前回の出力で今回生成されなかったファイルを削除")
    sp.add_argument("--include-account-notes", action="store_true",
                    help="アカウントの説明欄も出力する（パスワードが書かれている場合があるため既定は非出力）")
    sp.set_defaults(func=cmd_split)

    sp = sub.add_parser("nodes", help="指定パス配下の要素・属性を列挙")
    sp.add_argument("-i", "--input", required=True)
    sp.add_argument("path", help="例: /FMPReport/File/ScriptCatalog/*/Script")
    sp.add_argument("--non-empty", action="store_true",
                    help="子要素を持つ要素だけを対象にする（空の参照要素を除く）")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_nodes)

    sp = sub.add_parser("extract", help="指定パスの XML を切り出す")
    sp.add_argument("-i", "--input", required=True)
    sp.add_argument("path", help='例: /FMPReport/File/ScriptCatalog/Script:name="検索モード"')
    sp.add_argument("--inner", help='子孫の条件で絞る 例: */Script:name="共通処理"')
    sp.add_argument("--non-empty", action="store_true",
                    help="子要素を持つ要素だけを対象にする（空の参照要素を除く）")
    sp.add_argument("-o", "--out", help="出力ファイル（省略時は標準出力）")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser("xref", help="split 済み出力に対して相互参照を引く")
    sp.add_argument("-d", "--dir", required=True, help="split の出力先")
    sp.add_argument("--field", help='例: "TOP::顧客ID"（部分一致可）')
    sp.add_argument("--script")
    sp.add_argument("--layout")
    sp.add_argument("--to", help="テーブルオカレンス名")
    sp.add_argument("--table", help="BaseTable 名")
    sp.add_argument("--valuelist")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_xref)

    sp = sub.add_parser("audit", help="未使用スクリプト・値一覧などを洗い出す")
    sp.add_argument("-d", "--dir", required=True)
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("handover", help="split 済み出力から引き継ぎ資料の骨組みを作る")
    sp.add_argument("-d", "--dir", required=True, help="split の出力先")
    sp.add_argument("-o", "--out", required=True, help="資料の出力先ディレクトリ")
    sp.add_argument("--audience", default="all",
                    help="読者: all（既定） / dev / survey / client（カンマ区切り可）")
    sp.add_argument("--clean", action="store_true",
                    help="前回の出力で今回生成されなかったファイルを削除")
    sp.add_argument("--limit", type=int, default=60, help="棚卸し章の列挙件数")
    sp.set_defaults(func=cmd_handover)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        sys.exit(130)
