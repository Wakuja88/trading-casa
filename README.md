# trading-casa

Dashboard de trading en una sola página, con tabs:

| Tab | Qué es |
|---|---|
| Interés compuesto | Calculadora propia (`site/interes.html`) |
| Apalancamiento | Calculadora propia (`site/apalancamiento.html`): movimiento para la meta, liquidación, equivalencias y opciones por apalancamiento |
| Reloj de mercados | Reloj propio (`site/reloj.html`) con horarios y festivos de las bolsas |
| Equity Journal | Web app de Google Apps Script (pide sesión de Google) |
| Calendario económico | Forex Factory |
| Noticias cripto | Página propia (`site/noticias.html`): titulares de CoinDesk, Cointelegraph, The Block, Decrypt y Bitcoin Magazine, se refresca cada minuto |
| ETF BTC | Farside, flujos de los ETF de Bitcoin |
| Liquidaciones | Sub-tabs de Coinglass (`site/liquidaciones.html`): liquidaciones, mapa y heatmap BTC |

Abrir un tab directo: `#interes`, `#apalancamiento`, `#reloj`, `#equity`, `#calendario`, `#noticias`, `#etf`, `#liquidaciones`.

## Archivos
- `site/index.html`: la barra de tabs. Cada tab carga su página en un iframe. El botón "Abrir aparte ↗" abre el tab actual en otra pestaña.
- `site/*.html`: las herramientas propias y la página de sub-tabs de liquidaciones.
- `server.py`: sirve `site/`, guarda los escenarios del interés compuesto en `/var/lib/interes/escenarios.json` (`GET/PUT /api/escenarios`) y junta los feeds RSS de noticias en `GET /api/noticias` (caché de 60 s; la lista de medios está en `FEEDS`). Solo usa la librería estándar de Python.
- `deploy/`: servicio de systemd y auto-pull.

## Sitios externos
- Forex Factory y Coinglass no permiten mostrarse dentro de otra página. En la PC se ven gracias a una extensión de Chrome que quita ese bloqueo solo dentro de este dashboard. Sin la extensión (por ejemplo en el cel), usa "Abrir aparte ↗".
- El mapa de liquidaciones de Coinglass pide login, y el login no funciona embebido: ese sub-tab hay que abrirlo aparte.
- El Equity Journal necesita `setXFrameOptionsMode(ALLOWALL)` en su `doGet()` para poder mostrarse aquí. Al cambiar el script, publicar como **New version** del mismo deployment (no "New deployment", porque cambia la URL).

## Cómo se despliega
El servidor revisa este repo cada minuto (`trading-pull.timer`) y baja los cambios de `main`.
Los HTML se ven al recargar. Si cambia `server.py` o `deploy/trading.service`, el servicio se reinicia solo.

Ver qué se ha desplegado: `journalctl -u trading-pull -n 20`

Regresar a una versión anterior: en GitHub abre el archivo → History, copia la versión buena y súbela de nuevo. El servidor la baja en el siguiente minuto.
