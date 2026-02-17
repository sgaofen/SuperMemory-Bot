/**
 * 📔 日记本 — Diary Viewer App
 *
 * Client-side logic for calendar navigation, diary loading,
 * search, and dark mode.
 */

const API_BASE = '';

// ── State ────────────────────────────────────────────────────
let currentYear, currentMonth;
let allDiaries = [];
let daysWithDiary = [];
let selectedDate = null;

// ── DOM Refs ─────────────────────────────────────────────────
const calDays = document.getElementById('calDays');
const calTitle = document.getElementById('calTitle');
const diaryList = document.getElementById('diaryList');
const welcome = document.getElementById('welcome');
const diaryArticle = document.getElementById('diaryArticle');
const diaryContent = document.getElementById('diaryContent');
const noDiary = document.getElementById('noDiary');
const noDiaryDate = document.getElementById('noDiaryDate');
const searchInput = document.getElementById('searchInput');
const welcomeStats = document.getElementById('welcomeStats');
const themeToggle = document.getElementById('themeToggle');
const sidebar = document.getElementById('sidebar');
const mobileToggle = document.getElementById('mobileToggle');

// ── Init ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);

async function init() {
    // Set initial month to now
    const now = new Date();
    currentYear = now.getFullYear();
    currentMonth = now.getMonth() + 1;

    // Load theme
    const savedTheme = localStorage.getItem('diary-theme') || 'light';
    if (savedTheme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
        themeToggle.textContent = '☀️';
    }

    // Event listeners
    document.getElementById('prevMonth').addEventListener('click', () => navigateMonth(-1));
    document.getElementById('nextMonth').addEventListener('click', () => navigateMonth(1));
    themeToggle.addEventListener('click', toggleTheme);
    searchInput.addEventListener('input', debounce(handleSearch, 300));
    mobileToggle.addEventListener('click', () => sidebar.classList.toggle('open'));

    // Close sidebar on content click (mobile)
    document.getElementById('content').addEventListener('click', () => {
        sidebar.classList.remove('open');
    });

    // Load data
    await Promise.all([
        loadDiaryList(),
        loadCalendar(),
    ]);

    showWelcome();
}

// ── API Calls ────────────────────────────────────────────────

async function loadDiaryList() {
    try {
        const res = await fetch(`${API_BASE}/api/diaries`);
        allDiaries = await res.json();
        renderDiaryList(allDiaries);
    } catch (e) {
        console.error('Failed to load diaries:', e);
    }
}

async function loadCalendar() {
    try {
        const res = await fetch(`${API_BASE}/api/calendar?year=${currentYear}&month=${currentMonth}`);
        const data = await res.json();
        daysWithDiary = data.days_with_diary || [];
        renderCalendar();
    } catch (e) {
        console.error('Failed to load calendar:', e);
    }
}

async function loadDiary(dateStr) {
    try {
        const res = await fetch(`${API_BASE}/api/diary/${dateStr}`);
        if (res.ok) {
            const data = await res.json();
            return data.content || null;
        }
        return null;
    } catch (e) {
        console.error(`Failed to load diary for ${dateStr}:`, e);
        return null;
    }
}

// ── Calendar ─────────────────────────────────────────────────

function renderCalendar() {
    const monthNames = ['1月', '2月', '3月', '4月', '5月', '6月',
        '7月', '8月', '9月', '10月', '11月', '12月'];
    calTitle.textContent = `${currentYear}年${monthNames[currentMonth - 1]}`;

    const firstDay = new Date(currentYear, currentMonth - 1, 1).getDay();
    const daysInMonth = new Date(currentYear, currentMonth, 0).getDate();
    const today = new Date();
    const todayDay = today.getDate();
    const isCurrentMonth = today.getFullYear() === currentYear && today.getMonth() + 1 === currentMonth;

    let html = '';

    // Empty cells before first day
    for (let i = 0; i < firstDay; i++) {
        html += '<div class="cal-day empty"></div>';
    }

    // Days
    for (let d = 1; d <= daysInMonth; d++) {
        const dateStr = `${currentYear}-${String(currentMonth).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
        const hasDiary = daysWithDiary.includes(d);
        const isToday = isCurrentMonth && d === todayDay;
        const isActive = dateStr === selectedDate;

        let classes = 'cal-day';
        if (hasDiary) classes += ' has-diary';
        if (isToday) classes += ' today';
        if (isActive) classes += ' active';

        const onclick = hasDiary ? `onclick="selectDate('${dateStr}')"` : '';
        html += `<div class="${classes}" ${onclick}>${d}</div>`;
    }

    calDays.innerHTML = html;
}

function navigateMonth(delta) {
    currentMonth += delta;
    if (currentMonth > 12) {
        currentMonth = 1;
        currentYear++;
    } else if (currentMonth < 1) {
        currentMonth = 12;
        currentYear--;
    }
    loadCalendar();
}

// ── Diary List ───────────────────────────────────────────────

function renderDiaryList(entries) {
    const listHeader = '<div class="list-header">最近日记</div>';

    if (!entries || entries.length === 0) {
        diaryList.innerHTML = listHeader + '<div class="diary-item"><div class="diary-item-preview">还没有日记</div></div>';
        return;
    }

    let html = listHeader;
    for (const entry of entries) {
        const date = entry.date;
        const preview = entry.preview || '';
        const emoji = extractEmoji(preview) || '📝';
        const isActive = date === selectedDate ? ' active' : '';
        const displayDate = formatDateCN(date);

        html += `
            <div class="diary-item${isActive}" onclick="selectDate('${date}')" data-date="${date}">
                <div class="diary-item-date">
                    <span class="emoji">${emoji}</span>
                    ${displayDate}
                </div>
                <div class="diary-item-preview">${escapeHtml(preview)}</div>
            </div>
        `;
    }

    diaryList.innerHTML = html;
}

// ── View Control ─────────────────────────────────────────────

function showWelcome() {
    welcome.style.display = '';
    diaryArticle.style.display = 'none';
    noDiary.style.display = 'none';

    // Stats
    const totalDiaries = allDiaries.length;
    const totalChars = allDiaries.reduce((sum, d) => sum + (d.size || 0), 0);

    let statsHtml = '';
    if (totalDiaries > 0) {
        statsHtml += `
            <div class="stat-item">
                <div class="stat-number">${totalDiaries}</div>
                <div class="stat-label">篇日记</div>
            </div>
            <div class="stat-item">
                <div class="stat-number">${formatNumber(totalChars)}</div>
                <div class="stat-label">总字数</div>
            </div>
        `;
    }
    welcomeStats.innerHTML = statsHtml;
}

async function selectDate(dateStr) {
    selectedDate = dateStr;

    // Update calendar
    const dateObj = new Date(dateStr);
    const newYear = dateObj.getFullYear();
    const newMonth = dateObj.getMonth() + 1;
    if (newYear !== currentYear || newMonth !== currentMonth) {
        currentYear = newYear;
        currentMonth = newMonth;
        await loadCalendar();
    } else {
        renderCalendar();
    }

    // Update list selection
    document.querySelectorAll('.diary-item').forEach(el => {
        el.classList.toggle('active', el.dataset.date === dateStr);
    });

    // Close mobile sidebar
    sidebar.classList.remove('open');

    // Load diary
    const content = await loadDiary(dateStr);
    if (content) {
        showDiary(content);
    } else {
        showNoDiary(dateStr);
    }
}

function showDiary(markdownContent) {
    welcome.style.display = 'none';
    noDiary.style.display = 'none';
    diaryArticle.style.display = '';

    // Configure marked
    marked.setOptions({
        breaks: true,
        gfm: true,
    });

    diaryContent.innerHTML = marked.parse(markdownContent);

    // Scroll to top
    document.getElementById('content').scrollTop = 0;
}

function showNoDiary(dateStr) {
    welcome.style.display = 'none';
    diaryArticle.style.display = 'none';
    noDiary.style.display = '';
    noDiaryDate.textContent = formatDateCN(dateStr);
}

// ── Search ───────────────────────────────────────────────────

function handleSearch() {
    const query = searchInput.value.trim().toLowerCase();
    if (!query) {
        renderDiaryList(allDiaries);
        return;
    }

    const filtered = allDiaries.filter(d => {
        return d.date.includes(query) ||
            (d.preview && d.preview.toLowerCase().includes(query));
    });
    renderDiaryList(filtered);
}

// ── Theme ────────────────────────────────────────────────────

function toggleTheme() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (isDark) {
        document.documentElement.removeAttribute('data-theme');
        themeToggle.textContent = '🌙';
        localStorage.setItem('diary-theme', 'light');
    } else {
        document.documentElement.setAttribute('data-theme', 'dark');
        themeToggle.textContent = '☀️';
        localStorage.setItem('diary-theme', 'dark');
    }
}

// ── Helpers ──────────────────────────────────────────────────

function formatDateCN(dateStr) {
    try {
        const parts = dateStr.split('-');
        const year = parseInt(parts[0]);
        const month = parseInt(parts[1]);
        const day = parseInt(parts[2]);
        const d = new Date(year, month - 1, day);
        const weekdays = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];
        return `${month}月${day}日 ${weekdays[d.getDay()]}`;
    } catch {
        return dateStr;
    }
}

function extractEmoji(text) {
    if (!text) return null;
    const emojiRegex = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE00}-\u{FEFF}]/u;
    const match = text.match(emojiRegex);
    return match ? match[0] : null;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatNumber(n) {
    if (n >= 10000) {
        return (n / 10000).toFixed(1) + '万';
    }
    if (n >= 1000) {
        return (n / 1000).toFixed(1) + 'k';
    }
    return String(n);
}

function debounce(fn, delay) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), delay);
    };
}
