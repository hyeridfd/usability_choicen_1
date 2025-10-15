import streamlit as st
from datetime import datetime, timedelta
import pandas as pd
import os
import glob
import time
import base64, json

import os, base64
import streamlit as st
import streamlit.components.v1 as components
import re, unicodedata, time

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from io import BytesIO

# ===== Supabase helpers (ADD) ======================================
from supabase import create_client, Client

def peek_role(jwt: str):
    if not jwt or '.' not in jwt:
        return None, {"error":"invalid jwt"}
    payload = jwt.split('.')[1] + '=' * (-len(jwt.split('.')[1]) % 4)
    data = json.loads(base64.urlsafe_b64decode(payload))
    return data.get("role"), data

role, _ = peek_role(st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", ""))
st.write("JWT role =", role)   # 👉 반드시 'service_role' 이어야 합니다


# ✅ 캐시 무효화 가능한 버전 파라미터 추가
@st.cache_resource
def get_supabase(version: str = "v1") -> Client | None:
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_SERVICE_ROLE_KEY"]
        return create_client(url, key)
    except Exception:
        return None

# ✅ 새 키 반영하려면: secrets에서 버전만 바꿔주면 캐시가 재생성됨
sb = get_supabase(version=st.secrets.get("SUPABASE_CLIENT_VERSION", "v1"))

def _ascii_slug(s: str) -> str:
    # 한글/유니코드 제거 + 안전 문자만 남기기
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s)
    s = s.strip("-._")
    return s or "file"

def _storage_path(username: str, meal_type: str) -> str:
    u = _ascii_slug(username)
    m = _ascii_slug(meal_type)
    ts = time.strftime("%Y%m%d-%H%M%S")
    fname = f"{u}_{m}_{ts}.xlsx"
    # ✅ 버킷명 빼고, 버킷 내부 경로만
    return f"{u}/{time.strftime('%Y')}/{time.strftime('%m')}/{fname}"

def upload_to_storage(file_bytes: bytes, username: str, meal_type: str) -> str:
    sb = get_supabase(version=st.secrets.get("SUPABASE_CLIENT_VERSION", "v1"))
    if sb is None:
        raise RuntimeError("Supabase client not configured")

    bucket = st.secrets["SUPABASE_BUCKET"]  # 예: "submissions"
    path = _storage_path(username, meal_type)

    sb.storage.from_(bucket).upload(
        path=path,                      # 예: "SR12/2025/10/SR12_sikdanA_20251001-122941.xlsx"
        file=BytesIO(file_bytes),       # ✅ 파일 객체
        file_options={
            "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "cacheControl": "3600",
            "upsert": "true",
        },
    )
    return path


def insert_row_kor(username: str, started_at: datetime, submitted_at: datetime,
                   duration_sec: int, meal_type: str, storage_path: str, original_name: str):
    sb = get_supabase()
    if sb is None:
        raise RuntimeError("Supabase client not configured")
    row = {
        "사용자": username,
        "시작시간": started_at.isoformat(),
        "제출시간": submitted_at.isoformat(),
        "소요시간(초)": int(duration_sec),
        "식단표종류": meal_type,
        "파일경로": storage_path,
        "원본파일명": original_name,
    }
    sb.table("submissions").insert(row).execute()

def fetch_logs_df() -> pd.DataFrame:
    sb = get_supabase()
    if sb is None:
        return pd.DataFrame()
    res = sb.table("submissions").select("*").order("제출시간", desc=True).execute()
    return pd.DataFrame(res.data or [])

def make_signed_url(storage_path: str, expire_seconds: int = 3600) -> str:
    sb = get_supabase()
    if sb is None:
        return ""
    bucket = st.secrets["SUPABASE_BUCKET"]
    r = sb.storage.from_(bucket).create_signed_url(storage_path, expire_seconds)
    return r.get("signedURL") or r.get("signed_url") or ""
# ===================================================================


def render_index_html_with_injected_xlsx(
    html_height: int = 900,
    xlsx_candidates=None,
    html_file_path: str | None = None,
):
    """
    index.html 내용을 파이썬 문자열(INDEX_HTML)로 포함하고,
    존재하는 엑셀을 base64로 주입하여 Streamlit에서 바로 렌더합니다.

    - html_file_path 를 주면, 내부 문자열 대신 해당 파일 내용을 사용합니다(선택).
    - xlsx_candidates 순서대로 존재여부를 확인해 첫 번째 파일을 주입합니다.
    """
    if xlsx_candidates is None:
        xlsx_candidates = [
            "menu.xlsx",
            "/mnt/data/menu.xlsx",
            "/mnt/data/정선_음식 데이터_간식제외.xlsx",
        ]

    # 1) HTML 본문 준비 (파일 경로가 주어지면 파일 사용, 아니면 내장 문자열 사용)
    if html_file_path and os.path.exists(html_file_path):
        try:
            with open(html_file_path, "r", encoding="utf-8") as f:
                html_content = f.read()
        except Exception:
            with open(html_file_path, "r", encoding="cp949", errors="ignore") as f:
                html_content = f.read()
    else:
        html_content = INDEX_HTML  # 아래에 정의된 전체 HTML 문자열

    # 2) 엑셀 후보 중 첫 번째 존재 파일을 base64 인코딩
    xlsx_path = next((p for p in xlsx_candidates if os.path.exists(p)), None)
    if xlsx_path:
        with open(xlsx_path, "rb") as xf:
            b64 = base64.b64encode(xf.read()).decode()
        inject_script = f"<script>window.__XLSX_BASE64__='{b64}';</script>"
    else:
        # 주입 없음 → HTML 내부에서 fetch() 경로로 폴백
        inject_script = "<script>window.__XLSX_BASE64__=null;</script>"

    # 3) 주입 스크립트 + HTML 합치기 후 렌더
    final_html = inject_script + html_content
    components.html(final_html, height=html_height, scrolling=True)


# ==============================
# ↓↓↓ 여기부터 index.html 본문 ↓↓↓
# (주입(base64) 모드 + fetch 폴백 모두 지원)
# ==============================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1.0" />
  <title>메뉴 관리</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:'Malgun Gothic', sans-serif; background:#f5f5f5; color:#333; }
    .header { background:linear-gradient(to right,#5b6caa,#7b8bc4); color:#fff; padding:12px 20px; font-weight:700; display:flex; gap:10px; align-items:center; }
    .fics-logo { background:#3d4d7a; padding:4px 12px; border-radius:4px; }
    .container { background:#fff; margin:20px; border:1px solid #ddd; box-shadow:0 2px 4px rgba(0,0,0,.08); }
    .title-bar { background:#f8f9fa; padding:12px 16px; border-bottom:1px solid #e5e7eb; font-weight:700; }
    .content { padding:18px; }
    .info-section { display:flex; gap:20px; flex-wrap:wrap; margin-bottom:18px; padding:12px; background:#f8f9fa; border:1px solid #e5e7eb; border-radius:6px;}
    .info-item { display:flex; gap:8px; align-items:center; }
    .category-buttons { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px; }
    .category-btn { padding:8px 16px; border:1px solid #9ca3af; background:linear-gradient(to bottom,#fafafa,#e8e8e8); border-radius:6px; cursor:pointer; font-weight:600; }
    .category-btn.active { background:linear-gradient(to bottom,#5b6caa,#4a5a99); color:#fff; border-color:#3d4d7a; }
    .search-row { display:flex; gap:10px; margin-bottom:12px; align-items:center; }
    .search-input { flex:1; padding:10px 12px; border:1px solid #e5e7eb; border-radius:6px; }
    .search-btn { padding:10px 16px; border:none; border-radius:6px; background:#4a5a99; color:#fff; font-weight:700; cursor:pointer; }
    .filters { display:grid; grid-template-columns: repeat(4, minmax(160px,1fr)); gap:10px; margin-bottom:14px; }
    .filter { display:flex; flex-direction:column; gap:6px; }
    .filter label { font-size:12px; color:#6b7280; font-weight:700; }
    .filter select { padding:10px 12px; border:1px solid #e5e7eb; border-radius:6px; }
    .table-container { border:1px solid #e5e7eb; border-radius:8px; overflow:hidden; }
    table { width:100%; border-collapse:collapse; }
    thead { background:linear-gradient(to bottom,#6b7baa,#5b6b9a); color:#fff; }
    th, td { padding:12px 10px; text-align:center; border-bottom:1px solid #eef2f7; }
    tbody tr:hover { background:#f8fafc; }
    td.left { text-align:left; padding-left:18px; }
    .no-data { text-align:center; padding:36px 20px; color:#6b7280; }
    .no-data-icon { font-size:40px; opacity:.35; margin-bottom:8px; }
    .count-badge { background:#dc3545; color:#fff; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:700; }
  </style>
</head>
<body>
  <div class="header">
    <span class="fics-logo">FICS</span>
    <span>(주)초이스엔 메뉴 관리</span>
  </div>

  <div class="container">
    <div class="title-bar">메뉴관리</div>
    <div class="content">
      <div class="info-section">
        <div class="info-item"><strong>• 사업장:</strong> (주)초이스엔</div>
        <div class="info-item"><strong>• 업태/종목:</strong> 메뉴설계</div>
        <div class="info-item"><strong>• 총 메뉴 수:</strong> <span id="totalCount" class="count-badge">0</span></div>
      </div>

      <div class="category-buttons">
        <button class="category-btn" onclick="filterByCategory('all', event)">전체</button>
        <button class="category-btn" onclick="filterByCategory('밥', event)">밥</button>
        <button class="category-btn" onclick="filterByCategory('국', event)">국</button>
        <button class="category-btn" onclick="filterByCategory('주찬', event)">주찬</button>
        <button class="category-btn" onclick="filterByCategory('부찬', event)">부찬</button>
        <button class="category-btn" onclick="filterByCategory('김치', event)">김치</button>
      </div>

      <div class="search-row">
        <input id="searchInput" class="search-input" type="text" placeholder="메뉴명 검색…" oninput="applyAllFilters()" />
        <button class="search-btn" onclick="applyAllFilters()">검색</button>
      </div>

      <!-- 동적 드롭다운(카테고리 선택 시 노출) -->
      <div id="advancedFilters" class="filters" style="display:none;">
        <div class="filter">
          <label for="codeSelect">음식 분류코드</label>
          <select id="codeSelect" onchange="syncCascades(); applyAllFilters();"></select>
        </div>
        <div class="filter">
          <label for="largeSelect">대분류</label>
          <select id="largeSelect" onchange="syncCascades(); applyAllFilters();"></select>
        </div>
        <div class="filter">
          <label for="middleSelect">중분류</label>
          <select id="middleSelect" onchange="syncCascades(); applyAllFilters();"></select>
        </div>
        <div class="filter">
          <label for="cookSelect">조리법 유형</label>
          <select id="cookSelect" onchange="syncCascades(); applyAllFilters();"></select>
        </div>
      </div>

      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th style="width:60px;">#</th>
              <th>메뉴명</th>
              <th style="width:120px;">카테고리</th>
              <th style="width:140px;">음식 분류코드</th>
              <th style="width:140px;">대분류</th>
              <th style="width:140px;">중분류</th>
              <th style="width:140px;">조리법 유형</th>
            </tr>
          </thead>
          <tbody id="menuTableBody">
            <tr><td colspan="7" class="no-data"><div class="no-data-icon">📋</div>데이터를 불러오는 중…</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Streamlit이 넣어준 전역 주입값을 사용할 준비 -->
  <script>
    // Streamlit에서 window.__XLSX_BASE64__ 로 주입됨(없으면 null)
    const INJECTED_XLSX_BASE64 = (typeof window !== 'undefined' && window.__XLSX_BASE64__) ? window.__XLSX_BASE64__ : null;
  </script>

  <!-- SheetJS (XLSX 파서) -->
  <script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
  <script>
    let allMenuData = [];     // 전체 데이터
    let currentCategory = 'all';

    // XLSX 경로(정적 배포 시 사용) — Streamlit 주입이 없으면 사용됨
    const PRIMARY_XLSX  = './menu.xlsx';
    const FALLBACK_XLSX = encodeURI('./정선_음식 데이터_간식제외.xlsx');

    document.addEventListener('DOMContentLoaded', loadData);

    async function loadData() {
      // ✅ 1순위: Streamlit이 주입한 base64 엑셀
      if (INJECTED_XLSX_BASE64) {
        try {
          const binary = atob(INJECTED_XLSX_BASE64);
          const len = binary.length;
          const u8 = new Uint8Array(len);
          for (let i = 0; i < len; i++) u8[i] = binary.charCodeAt(i);
          const wb  = XLSX.read(u8, { type: 'array' });
          const sheet = wb.Sheets[wb.SheetNames[0]];
          const data = XLSX.utils.sheet_to_json(sheet, { defval: '' });
          hydrateData(data);
          return;
        } catch (e) {
          console.warn('Injected base64 parse failed, fallback to fetch()', e);
        }
      }

      // ✅ 2순위: 기존 방식(fetch)
      headExists(PRIMARY_XLSX).then(ok => ok ? parseXlsx(PRIMARY_XLSX)
        : headExists(FALLBACK_XLSX).then(ok2 => ok2 ? parseXlsx(FALLBACK_XLSX)
        : showError('XLSX 파일을 찾을 수 없습니다. menu.xlsx를 올렸는지 확인하세요.')));
    }

    function headExists(url) {
      return fetch(url, { method: 'HEAD' }).then(res => res.ok).catch(() => false);
    }

    async function parseXlsx(url) {
      try {
        const res = await fetch(url);
        const buf = await res.arrayBuffer();
        const wb  = XLSX.read(buf, { type: 'array' });
        const sheet = wb.Sheets[wb.SheetNames[0]];
        const data = XLSX.utils.sheet_to_json(sheet, { defval: '' });
        hydrateData(data);
      } catch (err) {
        showError('XLSX를 불러오지 못했습니다: ' + err);
      }
    }

    function hydrateData(data) {
      // 컬럼 매핑 (헤더명은 정확히 아래와 같아야 함)
      allMenuData = data.map(r => ({
        menu:   (r['Menu'] ?? '').toString().trim(),
        category: (r['Category'] ?? '').toString().trim(),
        code:   (r['음식 분류코드'] ?? '').toString().trim(),
        large:  (r['대분류'] ?? '').toString().trim(),
        middle: (r['중분류'] ?? '').toString().trim(),
        cook:   (r['조리법 유형'] ?? '').toString().trim()
      })).filter(x => x.menu);

      document.getElementById('totalCount').textContent = allMenuData.length.toLocaleString();

      // 초기 렌더: 전체
      setActiveCategoryButton('all');
      toggleAdvancedFilters(false);
      renderTable(allMenuData);
    }

    function setActiveCategoryButton(cat) {
      document.querySelectorAll('.category-btn').forEach(b => b.classList.remove('active'));
      const label = (cat === 'all') ? '전체' : cat;
      const btn = Array.from(document.querySelectorAll('.category-btn'))
        .find(b => b.textContent.replace(/\s+.*/, '') === label);
      if (btn) btn.classList.add('active');
    }

    function filterByCategory(cat) {
      currentCategory = cat;
      setActiveCategoryButton(cat);

      if (cat === 'all') {
        toggleAdvancedFilters(false);
      } else {
        toggleAdvancedFilters(true);
        populateAdvancedOptions();
      }
      applyAllFilters();
    }

    function toggleAdvancedFilters(show) {
      document.getElementById('advancedFilters').style.display = show ? 'grid' : 'none';
      if (!show) resetAdvancedSelects();
    }

    function resetAdvancedSelects() {
      ['codeSelect','largeSelect','middleSelect','cookSelect'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = `<option value="">전체</option>`;
      });
    }

    function getBaseRowsByCategory() {
      return currentCategory === 'all'
        ? allMenuData
        : allMenuData.filter(x => x.category === currentCategory);
    }

    /* ---------- 계단식(상호연동) 드롭다운 핵심 ---------- */
    function getCurrentFilters() {
      return {
        code:   (document.getElementById('codeSelect').value   || '').trim(),
        large:  (document.getElementById('largeSelect').value  || '').trim(),
        middle: (document.getElementById('middleSelect').value || '').trim(),
        cook:   (document.getElementById('cookSelect').value   || '').trim(),
      };
    }

    function allowedValues(rows, key) {
      return Array.from(new Set(rows.map(r => r[key]).filter(Boolean)))
        .sort((a,b) => a.localeCompare(b, 'ko'));
    }

    function setSelect(id, values, current) {
      const el = document.getElementById(id);
      const cur = current || '';
      el.innerHTML = `<option value="">전체</option>` +
        values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
      el.value = values.includes(cur) ? cur : '';
    }

    function recomputeOptions() {
      const base = getBaseRowsByCategory();
      const f    = getCurrentFilters();

      // 자기 자신을 제외한 나머지 선택 조건을 적용하여 허용값 계산
      const rowsExcept = (excludeKey) => base.filter(r =>
        (excludeKey === 'code'   || !f.code   || r.code   === f.code)   &&
        (excludeKey === 'large'  || !f.large  || r.large  === f.large)  &&
        (excludeKey === 'middle' || !f.middle || r.middle === f.middle) &&
        (excludeKey === 'cook'   || !f.cook   || r.cook   === f.cook)
      );

      const codeAllowed   = allowedValues(rowsExcept('code'),   'code');
      const largeAllowed  = allowedValues(rowsExcept('large'),  'large');
      const middleAllowed = allowedValues(rowsExcept('middle'), 'middle');
      const cookAllowed   = allowedValues(rowsExcept('cook'),   'cook');

      setSelect('codeSelect',   codeAllowed,   f.code);
      setSelect('largeSelect',  largeAllowed,  f.large);
      setSelect('middleSelect', middleAllowed, f.middle);
      setSelect('cookSelect',   cookAllowed,   f.cook);
    }

    function populateAdvancedOptions() {
      // 카테고리 전환 시 선택 초기화 후 가능한 값 계산
      ['codeSelect','largeSelect','middleSelect','cookSelect'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      recomputeOptions();
    }

    function syncCascades() {
      recomputeOptions();
    }
    /* --------------------------------------------------- */

    function applyAllFilters() {
      const kw = document.getElementById('searchInput').value.trim().toLowerCase();
      const f  = getCurrentFilters();

      let rows = getBaseRowsByCategory();
      if (kw)      rows = rows.filter(r => r.menu.toLowerCase().includes(kw));
      if (f.code)  rows = rows.filter(r => r.code   === f.code);
      if (f.large) rows = rows.filter(r => r.large  === f.large);
      if (f.middle)rows = rows.filter(r => r.middle === f.middle);
      if (f.cook)  rows = rows.filter(r => r.cook   === f.cook);

      renderTable(rows);
    }

    function renderTable(rows) {
      const tbody = document.getElementById('menuTableBody');

      if (!rows || rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="no-data"><div class="no-data-icon">🔍</div>표시할 메뉴가 없습니다.</td></tr>`;
        return;
      }

      tbody.innerHTML = rows.map((r, i) => `
        <tr>
          <td>${i + 1}</td>
          <td class="left">${escapeHtml(r.menu)}</td>
          <td>${escapeHtml(r.category)}</td>
          <td>${escapeHtml(r.code)}</td>
          <td>${escapeHtml(r.large)}</td>
          <td>${escapeHtml(r.middle)}</td>
          <td>${escapeHtml(r.cook)}</td>
        </tr>
      `).join('');
    }

    function showError(msg) {
      document.getElementById('menuTableBody').innerHTML =
        `<tr><td colspan="7" class="no-data"><div class="no-data-icon">⚠️</div>${msg}</td></tr>`;
    }

    function escapeHtml(s) {
      return (s || '').toString().replace(/[&<>"']/g, m => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
      })[m]);
    }
  </script>
</body>
</html>
"""
# ==============================
# ↑↑↑ index.html 본문 끝 ↑↑↑
# ==============================

# 페이지 설정
st.set_page_config(
    page_title="통합 식단 관리 시스템",
    page_icon="🍽️",
    layout="wide",
    initial_sidebar_state="expanded"
)

TEMPLATE_FOLDER = "templates"
os.makedirs(TEMPLATE_FOLDER, exist_ok=True)

# 커스텀 CSS
st.markdown("""
<style>
/* 메인 배경 */
.main {
    padding: 2rem 3rem;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    min-height: 100vh;
}
.card {
    background: white;
    padding: 2rem;
    border-radius: 15px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.1);
    margin-bottom: 2rem;
    border: 1px solid rgba(255,255,255,0.2);
}
.login-card {
    max-width: 400px;
    margin: 5rem auto;
    background: white;
    padding: 3rem;
    border-radius: 20px;
    box-shadow: 0 15px 35px rgba(0,0,0,0.1);
    text-align: center;
}
.success-banner {
    background: linear-gradient(90deg, #56ab2f, #a8e6cf);
    color: white;
    padding: 1rem;
    border-radius: 10px;
    text-align: center;
    font-weight: 600;
    margin-bottom: 2rem;
}
.admin-header {
    background: linear-gradient(90deg, #667eea, #764ba2);
    color: white;
    padding: 1.5rem;
    border-radius: 10px;
    text-align: center;
    margin-bottom: 2rem;
}
.user-header {
    background: linear-gradient(90deg, #4facfe, #00f2fe);
    color: white;
    padding: 1.5rem;
    border-radius: 10px;
    text-align: center;
    margin-bottom: 2rem;
}
.stButton > button {
    background: linear-gradient(45deg, #667eea, #764ba2);
    color: white;
    border: none;
    padding: 0.75rem 2rem;
    border-radius: 25px;
    font-weight: 600;
    transition: all 0.3s ease;
    box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
}
.stButton > button:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px rgba(102, 126, 234, 0.4);
}
.danger-button button {
    background: linear-gradient(45deg, #ff416c, #ff4b2b) !important;
    box-shadow: 0 4px 15px rgba(255, 65, 108, 0.3) !important;
}
.start-button button {
    background: linear-gradient(45deg, #56ab2f, #a8e6cf) !important;
    box-shadow: 0 4px 15px rgba(86, 171, 47, 0.3) !important;
    font-size: 1.1rem !important;
    padding: 1rem 2.5rem !important;
}
.stat-card {
    background: white;
    padding: 1.5rem;
    border-radius: 10px;
    text-align: center;
    box-shadow: 0 4px 15px rgba(0,0,0,0.1);
    margin: 0.5rem;
}
.stat-number {
    font-size: 2rem;
    font-weight: 700;
    color: #667eea;
}
.stat-label {
    color: #7f8c8d;
    font-size: 0.9rem;
    margin-top: 0.5rem;
}
/* 사이드바 스타일 */
.css-1d391kg {
    background: linear-gradient(180deg, #667eea 0%, #764ba2 100%);
}
section[data-testid="stSidebar"] > div {
    background: linear-gradient(180deg, #667eea 0%, #764ba2 100%);
}
</style>
""", unsafe_allow_html=True)

LOG_FILE = "log.csv"
UPLOAD_FOLDER = "uploads"
HTML_FILE = "index.html"
MENU_XLSX = "menu.xlsx"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 사용자 설정
user_dict = {
    "SR01": "test01", "SR02": "test02", "SR03": "test03", "SR04": "test04",
    "SR05": "test05", "SR06": "test06", "SR07": "test07", "SR08": "test08",
    "SR09": "test09", "SR10": "test10", "SR11": "test11", "SR12": "test12",
    "SR13": "test13", "admin": "admin"
}

def get_kst_now():
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul"))

# 템플릿 파일 다운로드 함수
import requests

def get_template_file(meal_type: str) -> bytes | None:
    """
    템플릿 파일을 반환합니다(바이트).
    1) templates/ 에 로컬 파일이 있으면 그걸 사용
    2) 없으면 GitHub raw에서 다운로드 후 templates/에 저장하고 반환
    """
    # 1) 파일명(베이스네임)만 매핑
    template_files = {
        "식단표A": "식단표 A.xlsx",
        "식단표B": "식단표 B.xlsx",
    }

    filename = template_files.get(meal_type)
    if not filename:
        return None

    local_path = os.path.join(TEMPLATE_FOLDER, filename)

    # 2) 로컬에 있으면 즉시 반환
    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            return f.read()

    # 3) 로컬에 없으면 GitHub raw에서 다운로드
    #   ⚠️ 반드시 raw.githubusercontent.com 사용 (blob 아님)
    github_raw_urls = {
        "식단표A": "https://raw.githubusercontent.com/hyeridfd/usability_choicen/main/templates/%EC%8B%9D%EB%8B%A8%ED%91%9C%20A.xlsx",
        "식단표B": "https://raw.githubusercontent.com/hyeridfd/usability_choicen/main/templates/%EC%8B%9D%EB%8B%A8%ED%91%9C%20B.xlsx",
    }
    url = github_raw_urls.get(meal_type)
    if not url:
        return None

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.content
        # 4) 받은 파일을 templates/에 저장
        with open(local_path, "wb") as f:
            f.write(data)
        return data
    except Exception as e:
        st.error(f"템플릿 다운로드 실패: {e}")
        return None

    
# 초기 상태
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.username = ""
    st.session_state.start_time = None
if "meal_type" not in st.session_state:
    st.session_state.meal_type = "식단표A"

# # HTML 파일을 읽어서 iframe으로 표시하는 함수
# def render_index_html_with_injected_xlsx():
#     html_path = "index.html"  # 지금 쓰시는 경로
#     if not os.path.exists(html_path):
#         st.error("⚠️ index.html 파일을 찾을 수 없습니다. 파일을 같은 디렉토리에 배치해주세요.")
#         return

#     # 1) HTML 읽기
#     try:
#         with open(html_path, 'r', encoding='utf-8') as f:
#             html_content = f.read()
#     except:
#         with open(html_path, 'r', encoding='cp949', errors='ignore') as f:
#             html_content = f.read()

#     # 2) Excel 후보 경로 중 존재하는 것 선택
#     xlsx_candidates = [
#         "menu.xlsx",
#         "/mnt/data/menu.xlsx",
#         "/mnt/data/정선_음식 데이터_간식제외.xlsx"
#     ]
#     xlsx_path = next((p for p in xlsx_candidates if os.path.exists(p)), None)

#     # 3) base64로 주입 스크립트 생성
#     inject_script = ""
#     if xlsx_path:
#         with open(xlsx_path, "rb") as xf:
#             b64 = base64.b64encode(xf.read()).decode()
#         inject_script = f"<script>window.__XLSX_BASE64__='{b64}';</script>"
#     else:
#         # 주입이 없으면 null로 선언 (HTML이 fetch()로 폴백)
#         inject_script = "<script>window.__XLSX_BASE64__=null;</script>"

#     # 4) 주입 스크립트 + 기존 HTML을 합쳐서 iframe 렌더
#     final_html = inject_script + html_content
#     st.components.v1.html(final_html, height=900, scrolling=True)

# 사이드바 - 탭 선택
with st.sidebar:
    st.markdown("""
    <div style='text-align: center; padding: 2rem 0;'>
        <h1 style='color: white; margin-bottom: 0.5rem;'>🍽️</h1>
        <h2 style='color: white; font-size: 1.5rem;'>통합 식단 시스템</h2>
    </div>
    """, unsafe_allow_html=True)
    
    if st.session_state.logged_in:
        st.markdown(f"""
        <div style='background: rgba(255,255,255,0.2); padding: 1rem; border-radius: 10px; text-align: center; color: white; margin-bottom: 2rem;'>
            <strong>👤 {st.session_state.username}</strong>
        </div>
        """, unsafe_allow_html=True)
    
    # ✅ 사이드바 - 예쁜 탭 UI
    selected_tab = st.radio("메뉴 선택", ["📝 식단 제출", "🔍 메뉴 관리"], label_visibility="collapsed")

    st.markdown("<br><br>", unsafe_allow_html=True)

    # # 클릭에 따라 세션상태 변경
    # if "active_tab" not in st.session_state:
    #     st.session_state.active_tab = "submit"

    # tab_html = f"""
    # <div class="menu-tab">
    #   <div class="menu-item {'active' if st.session_state.active_tab=='submit' else ''}" 
    #        onclick="window.parent.postMessage({{'tab':'submit'}}, '*')">
    #     <span class="menu-icon">📝</span> 식단 제출
    #   </div>
    #   <div class="menu-item {'active' if st.session_state.active_tab=='menu' else ''}" 
    #        onclick="window.parent.postMessage({{'tab':'menu'}}, '*')">
    #     <span class="menu-icon">🔍</span> 메뉴 관리
    #   </div>
    # </div>
    # """
    # st.markdown(tab_html, unsafe_allow_html=True)

    # JS → Streamlit 세션 업데이트용 스크립트
    st.markdown("""
    <script>
    window.addEventListener("message", (e) => {
        if (e.data.tab) {
            window.parent.postMessage({tab: e.data.tab}, "*");
        }
    });
    </script>
    """, unsafe_allow_html=True)

    
    st.markdown("<br><br>", unsafe_allow_html=True)
    
    if st.session_state.logged_in:
        if st.button("🚪 로그아웃", use_container_width=True):
            st.session_state.logged_in = False
            st.session_state.username = ""
            st.session_state.start_time = None
            st.session_state.meal_type = "식단표A"
            st.rerun()

# 로그인 화면
if not st.session_state.logged_in:
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("""
    <div class="login-card">
        <h1 style="color: #667eea; margin-bottom: 0.5rem;">🍽️</h1>
        <h2 style="color: #2c3e50; margin-bottom: 0.5rem;">식단 설계 시스템</h2>
        <p style="color: #7f8c8d; margin-bottom: 2rem;">로그인하여 시작하세요</p>
    </div>
    """, unsafe_allow_html=True)
    
    with st.container():
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            username = st.text_input("👤 아이디", placeholder="아이디를 입력하세요")
            password = st.text_input("🔒 비밀번호", type="password", placeholder="비밀번호를 입력하세요")
            
            if st.button("🚀 로그인", use_container_width=True):
                if username in user_dict and user_dict[username] == password:
                    st.session_state.logged_in = True
                    st.session_state.username = username
                    st.rerun()
                else:
                    st.error("❌ 아이디 또는 비밀번호가 올바르지 않습니다.")

# 로그인 후 화면
else:
    st.markdown(f"""
    <div class="success-banner">
        🎉 {st.session_state.username}님 환영합니다!
    </div>
    """, unsafe_allow_html=True)
    
    # 탭 1: 식단 제출
    if selected_tab == "📝 식단 제출":
        # 관리자 페이지
        if st.session_state.username == "admin":
            st.markdown("""
            <div class="admin-header">
                <h1>🔧 관리자 페이지</h1>
                <p>시스템 관리 및 제출 기록 확인</p>
            </div>
            """, unsafe_allow_html=True)
            
            # 통계 카드 + 표
            sb = get_supabase()
            df_db = fetch_logs_df() if sb else pd.DataFrame()
            
            if not df_db.empty:
                # Supabase 기준 통계
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.markdown(f"""<div class="stat-card"><div class="stat-number">{len(df_db)}</div><div class="stat-label">총 제출 수</div></div>""", unsafe_allow_html=True)
                with col2:
                    st.markdown(f"""<div class="stat-card"><div class="stat-number">{df_db['사용자'].nunique()}</div><div class="stat-label">참여 사용자</div></div>""", unsafe_allow_html=True)
                with col3:
                    avg_time = int(df_db["소요시간(초)"].mean()) if "소요시간(초)" in df_db.columns else 0
                    st.markdown(f"""<div class="stat-card"><div class="stat-number">{avg_time}초</div><div class="stat-label">평균 소요시간</div></div>""", unsafe_allow_html=True)
                with col4:
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    today_count = df_db["제출시간"].astype(str).str.contains(today_str).sum()
                    st.markdown(f"""<div class="stat-card"><div class="stat-number">{today_count}</div><div class="stat-label">오늘 제출</div></div>""", unsafe_allow_html=True)
            
                st.markdown("""<div class="card"><h3>📊 제출 기록</h3></div>""", unsafe_allow_html=True)
                show_cols = ["사용자","시작시간","제출시간","소요시간(초)","식단표종류","파일경로","원본파일명"]
                st.dataframe(df_db[[c for c in show_cols if c in df_db.columns]], use_container_width=True)
            
                st.markdown("<br>", unsafe_allow_html=True)
                users = df_db["사용자"].unique().tolist()
                sel_user = st.selectbox("👤 사용자 선택", users)
            
                user_rows = df_db[df_db["사용자"] == sel_user].sort_values("제출시간", ascending=False)
                for _, r in user_rows.iterrows():
                    label = f"📥 {r.get('원본파일명','제출파일')} ({r['식단표종류']} / {str(r['제출시간'])[:19]})"
                    signed = make_signed_url(r["파일경로"], expire_seconds=3600)
                    if signed:
                        st.link_button(label, url=signed, use_container_width=True)
                    else:
                        st.warning(f"URL 생성 실패 또는 로컬 파일만 존재: {r['파일경로']}")
            else:
                # 폴백: 기존 log.csv + 로컬 다운로드
                if os.path.exists(LOG_FILE):
                    df = pd.read_csv(LOG_FILE)
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.markdown(f"""<div class="stat-card"><div class="stat-number">{len(df)}</div><div class="stat-label">총 제출 수</div></div>""", unsafe_allow_html=True)
                    with col2:
                        st.markdown(f"""<div class="stat-card"><div class="stat-number">{df['사용자'].nunique()}</div><div class="stat-label">참여 사용자</div></div>""", unsafe_allow_html=True)
                    with col3:
                        avg_time = int(df['소요시간(초)'].mean()) if '소요시간(초)' in df.columns else 0
                        st.markdown(f"""<div class="stat-card"><div class="stat-number">{avg_time}초</div><div class="stat-label">평균 소요시간</div></div>""", unsafe_allow_html=True)
                    with col4:
                        today_str = datetime.now().strftime('%Y-%m-%d')
                        today_count = len(df[df['제출시간'].astype(str).str.contains(today_str)])
                        st.markdown(f"""<div class="stat-card"><div class="stat-number">{today_count}</div><div class="stat-label">오늘 제출</div></div>""", unsafe_allow_html=True)
            
                    st.markdown("""<div class="card"><h3>📊 제출 기록</h3></div>""", unsafe_allow_html=True)
                    st.dataframe(df, use_container_width=True)
            
                    st.markdown("<br>", unsafe_allow_html=True)
                    col1, col2 = st.columns(2)
                    with col1:
                        user_list = df["사용자"].unique().tolist()
                        selected_user = st.selectbox("👤 사용자 선택", user_list)
                    with col2:
                        pattern = os.path.join(UPLOAD_FOLDER, f"{selected_user}_식단표*.xlsx")
                        files = sorted(glob.glob(pattern))
                        if files:
                            for path in files:
                                base = os.path.basename(path)
                                label = f"📥 {os.path.splitext(base)[0]} 다운로드"
                                with open(path, "rb") as f:
                                    st.download_button(label=label, data=f, file_name=base, use_container_width=True)
                        else:
                            st.warning(f"⚠️ {selected_user}님의 제출 파일이 존재하지 않습니다.")
                else:
                    st.info("📝 제출 기록이 아직 없습니다.")

        
        # 사용자 페이지
        else:
            # 템플릿 다운로드 섹션 추가
            st.markdown("""
            <div class="card">
                <h3>📥 식단표 템플릿 다운로드</h3>
                <p>작업에 필요한 식단표 템플릿을 먼저 다운로드하세요.</p>
            </div>
            """, unsafe_allow_html=True)
            
            col1, col2 = st.columns(2)
            with col1:
                # 식단표A 다운로드
                template_a = get_template_file("식단표A")
                if template_a:
                    st.download_button(
                        label="📊 식단표 A 다운로드",
                        data=template_a,
                        file_name="식단표_A_템플릿.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.info("📋 식단표 A 템플릿을 templates/ 폴더에 배치해주세요")
            
            with col2:
                # 식단표B 다운로드
                template_b = get_template_file("식단표B")
                if template_b:
                    st.download_button(
                        label="📊 식단표 B 다운로드",
                        data=template_b,
                        file_name="식단표_B_템플릿.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                else:
                    st.info("📋 식단표 B 템플릿을 templates/ 폴더에 배치해주세요")
            
            st.markdown("<br>", unsafe_allow_html=True)
            # 식단표 선택
            st.markdown("""
            <div class="card">
                <h3>🧾 식단표 선택</h3>
                <p>작업하실 식단표를 먼저 선택해주세요.</p>
            </div>
            """, unsafe_allow_html=True)
            
            st.session_state.meal_type = st.radio(
                "식단표 유형",
                options=["식단표A", "식단표B"],
                index=0 if st.session_state.meal_type == "식단표A" else 1,
                horizontal=True
            )
            
            # 시작 버튼 섹션
            st.markdown("""
            <div class="card">
                <h3>🚀 작업 시작</h3>
                <p>아래 버튼을 클릭하여 식단 개선 작업을 시작하세요.</p>
            </div>
            """, unsafe_allow_html=True)
            
            if st.session_state.start_time is None:
                col1, col2 = st.columns([1, 1])
                with col1:
                    st.markdown('<div class="start-button">', unsafe_allow_html=True)
                    if st.button("🍽️ 식단 설계 시작", use_container_width=True):
                        st.session_state.start_time = get_kst_now()
                        st.success(f"⏰ 시작 시간: {st.session_state.start_time.strftime('%H:%M:%S')}")
                        st.rerun()
                    st.markdown('</div>', unsafe_allow_html=True)
            else:
                # 진행 중 상태 표시
                current_time = get_kst_now()
                elapsed = current_time - st.session_state.start_time
                elapsed_seconds = int(elapsed.total_seconds())
                
                st.markdown(f"""
                <div style="background: linear-gradient(90deg, #56ab2f, #a8e6cf); color: white; padding: 1rem; border-radius: 10px; text-align: center; margin-bottom: 2rem;">
                    ⏱️ 작업 진행 중... | 시작 시간: {st.session_state.start_time.strftime('%H:%M:%S')} | 경과 시간: {elapsed_seconds}초 | 선택: {st.session_state.meal_type}
                </div>
                """, unsafe_allow_html=True)
                
                # 파일 업로드 섹션
                st.markdown("""
                <div class="card">
                    <h3>📁 파일 업로드</h3>
                    <p>완성된 식단 설계 엑셀 파일을 업로드해주세요.</p>
                </div>
                """, unsafe_allow_html=True)
                
                uploaded_file = st.file_uploader(
                    "📊 엑셀 파일 선택",
                    type=["xlsx", "xls"],
                    help="xlsx 또는 xls 파일만 업로드 가능합니다."
                )
                
                if uploaded_file:
                    st.success(f"✅ 파일 선택됨: {uploaded_file.name}")
                
                    col1, col2, col3 = st.columns([1, 2, 1])
                    with col2:
                        if st.button("📤 제출하기", use_container_width=True):
                            # --- 필수: 여기서 모두 지역 변수로 만든다 ---
                            submit_time  = get_kst_now()
                            started_at   = st.session_state.start_time or submit_time
                            duration_sec = max(0, int((submit_time - started_at).total_seconds()))
                            username     = st.session_state.username
                            safe_meal    = st.session_state.meal_type
                            save_name    = f"{username}_{safe_meal}.xlsx"
                
                            # 파일 바이트
                            file_bytes = uploaded_file.read()
                
                            # (선택) Supabase 업로드 시도 후 storage_path 설정
                            storage_path = ""
                            sb = get_supabase() if "get_supabase" in globals() else None
                            if sb:
                                try:
                                    storage_path = upload_to_storage(file_bytes, username, safe_meal)
                                except Exception as e:
                                    st.warning(f"Supabase 업로드 실패(로컬 저장으로 대체): {e}")
                
                            # 로컬에도 저장(폴백/백업)
                            file_path = os.path.join(UPLOAD_FOLDER, save_name)
                            with open(file_path, "wb") as f:
                                f.write(file_bytes)
                
                            # 로그 CSV 갱신
                            log_row = {
                                "사용자": username,
                                "시작시간": started_at.strftime('%Y-%m-%d %H:%M:%S'),
                                "제출시간": submit_time.strftime('%Y-%m-%d %H:%M:%S'),
                                "소요시간(초)": duration_sec,
                                "식단표종류": safe_meal,
                                "파일경로": storage_path or file_path,  # Supabase 경로 우선
                            }
                            if os.path.exists(LOG_FILE):
                                existing = pd.read_csv(LOG_FILE)
                                for col in ["파일경로", "식단표종류"]:
                                    if col not in existing.columns:
                                        existing[col] = None
                                log_df = pd.concat([existing, pd.DataFrame([log_row])], ignore_index=True)
                            else:
                                log_df = pd.DataFrame([log_row])
                            log_df.to_csv(LOG_FILE, index=False)
                
                            # (선택) Supabase DB 로그
                            if sb and storage_path:
                                try:
                                    insert_row_kor(username, started_at, submit_time, duration_sec, safe_meal, storage_path, uploaded_file.name)
                                except Exception as e:
                                    st.warning(f"Supabase 로그 적재 실패: {e}")
                
                            # 완료 메시지 (여기서 지역 변수만 사용!)
                            st.success("🎉 제출이 완료되었습니다!")
                            st.markdown(f"""
                            <div style="background: #e8f5e8; padding: 1.5rem; border-radius: 10px; margin: 1rem 0;">
                                <h4>📋 제출 완료 요약</h4>
                                <p><strong>👤 사용자:</strong> {username}</p>
                                <p><strong>🧾 식단표:</strong> {safe_meal}</p>
                                <p><strong>⏰ 소요 시간:</strong> {duration_sec}초</p>
                                <p><strong>📅 제출 시간:</strong> {submit_time.strftime('%Y-%m-%d %H:%M:%S')}</p>
                                <p><strong>💾 저장 파일명:</strong> {save_name}</p>
                                <p><strong>🗄️ 저장 위치:</strong> {storage_path or file_path}</p>
                            </div>
                            """, unsafe_allow_html=True)
                
                            # 세션 리셋
                            st.session_state.start_time = None


    # 탭 2: 메뉴 관리
    elif selected_tab == "🔍 메뉴 관리":
        # st.markdown("""
        # <div class="user-header">
        #     <h1>🔍 메뉴 관리</h1>
        #     <p>메뉴 데이터베이스 조회 및 검색</p>
        # </div>
        # """, unsafe_allow_html=True)
        
        render_index_html_with_injected_xlsx()
