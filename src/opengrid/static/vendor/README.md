# Local browser dependencies

These pinned, open-source browser distributions are checked in so the application
does not use a runtime CDN or need an internet connection. Update deliberately and
run the browser tests after changing a version. The original license files are
included beside each library. The application does not use Alpine or a SPA runtime.

| File | Upstream version | License | Download source |
| --- | --- | --- | --- |
| `htmx-2.0.11.min.js` | HTMX 2.0.11 | Zero-Clause BSD | https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.11/dist/htmx.min.js |
| `chart-4.5.1.umd.min.js` | Chart.js 4.5.1 | MIT | https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js |

The Chart.js bundle includes `@kurkle/color` 0.3.2; its MIT license is included as
`LICENSE.kurkle-color.txt` from https://github.com/kurkle/color/tree/v0.3.2.

Chart.js lists jsDelivr as an official distribution method:
https://www.chartjs.org/docs/latest/getting-started/installation

SHA-256 checksums of the downloaded files:

```text
48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a  chart-4.5.1.umd.min.js
d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717  htmx-2.0.11.min.js
```
