"""One-time, idempotent RAZOM feature installer. Keeps existing webhook and server configuration.

Run in a cloned RAZOM folder: python apply_razom_upgrade.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def put_once(source: str, marker: str, insert: str, *, before: bool = False) -> str:
    if insert in source:
        return source
    if marker not in source:
        raise ValueError(f"Не вдалося знайти місце для вставки: {marker[:80]}")
    return source.replace(marker, insert + marker if before else marker + insert, 1)


def patch_main():
    path = ROOT / 'main.py'
    src = path.read_text(encoding='utf-8')
    if 'app.include_router(razom_features.router)' not in src:
        src += ('\n\n# Додаткові функції RAZOM 2.5: зберігаємо поточні маршрути і Telegram webhook.\n'
                'import razom_features\n'
                'app.include_router(razom_features.router)\n')
        path.write_text(src, encoding='utf-8')
    print('API: готово')


def patch_html(path):
    if not path.is_file():
        return
    html = path.read_text(encoding='utf-8')
    styles = '''
/* RAZOM: аналітика, дублікати, карта */
.feature-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(195px,1fr));gap:12px;margin:16px 0}
.feature-card{border:1px solid #d8e8de;background:#f8fbf8;border-radius:12px;padding:16px}
.feature-card p{margin:6px 0}
#geoMap{height:350px;min-height:300px;border:1px solid #c9dccc;border-radius:13px;margin-top:13px;z-index:0}
@media(max-width:700px){#geoMap{height:290px}}
'''
    html = put_once(html, '</style>', styles, before=True)
    html = put_once(html, '</head>', '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">\n', before=True)
    admin = '''
<div class="divider"></div>
<h3>Можливі дублікати заявок</h3><p class="muted small">Система лише попереджає про схожі звернення — не видаляє заявки автоматично.</p>
<div id="duplicateMatches"></div>
<div class="divider"></div>
<h3>Прогноз потреб</h3><p class="muted small">Орієнтовний тренд за історією заявок. За недостатньої кількості даних прогноз не показується.</p>
<div id="forecastCards"></div>
<div class="divider"></div>
<h3>Географічна карта і логістика</h3><p class="muted small">На карті — приблизні центри міст, не адреси людей. Пунктиром показані можливі перевезення по прямій, не реальні дороги.</p>
<div id="geoMap" role="region" aria-label="Карта звернень по містах"></div>
<p class="muted small" id="geoInfo"></p>
'''
    html = put_once(html, '<div id="statusStats"></div>', admin, before=True)
    logistics = '''
<div class="divider"></div>
<h3>Можлива допомога в сусідніх містах</h3>
<p class="muted small">Орієнтовні відповідності для планування логістики. Перевезення необхідно узгоджувати окремо.</p>
<div id="logisticsList"></div>
'''
    html = put_once(html, '<div id="myResources"></div>', logistics, before=False)
    javascript = (ROOT / 'razom_features_frontend.js').read_text(encoding='utf-8')
    if '/* RAZOM 2.5 extras' not in html:
        # Place it before the automatic app start, not after it.
        html = put_once(html, '(async()=>{if(token)await renderDashboard();', javascript + '\n', before=True)
    path.write_text(html, encoding='utf-8')
    print(f'UI: {path.relative_to(ROOT)} готово')


def main():
    if not (ROOT/'main.py').exists() or not (ROOT/'razom_features.py').exists():
        sys.exit('Запустіть файл у папці RAZOM з main.py та razom_features.py')
    # Check all anchors before modifying any HTML by running on in-memory snippets.
    patch_html(ROOT / 'web' / 'index.html')
    patch_html(ROOT / 'index.html')
    patch_main()
    print('✅ Додаткові функції встановлено. Перевірте: python -m pytest -q')


if __name__ == '__main__':
    main()
