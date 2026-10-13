# Examples

These pages are real output from the toolkit skills, so you can see what you get before pointing the toolkit at your own Looker repo.

| File | Produced by | What it shows |
|------|-------------|---------------|
| `sample-erd.html` | `/looker-erd` | 13 explores, 27 unique views and 46 joins, with three `extends:` relationships |
| `sample-sankey.html` | `/sankey-diagram` | 10 LookML dashboards flowing into the explores and views they use |

Open either file in a browser. Both load their charting library (D3 or ECharts) from a CDN, so you need to be online.

## Source project

Both examples were generated from the public **thelook eCommerce** LookML project:

- Repository: <https://github.com/looker-open-source/thelookevent>
- Commit: `611d788ef7f96b7659ab78c72898c8ac81e5ab5c`
- Licence: MIT

No LookML from that project is included in this repository. The HTML pages contain only metadata derived from it: explore, view and dashboard names, file paths, line and join counts.

## Things to know when reading them

- **Sankey priorities are illustrative.** LookML carries no P0 to P4 priority, so the priorities in `sample-sankey.html` were assigned by hand to show how the priority filter works. On a real migration they come from your own prioritisation.
- **The Sankey shows LookML dashboards only.** thelook has no user-generated (UGC) dashboards in its repo, which is exactly the coverage gap the main README warns about. On a real estate, run `queries/system_activity_tile_count.sql` to find the dashboards the repo cannot show.
- **Every explore scores low risk.** thelook is a compact demo model (its largest explore is 141 lines with 9 joins), well under the medium threshold. Production models tend to light up orange and red.
- **Every view is grey ("Other View").** The ERD colours views by `fact_`, `dim_`, `agg_` and `dt_` prefixes, and thelook uses none of them.
- **Dashboard to explore links include dashboard filters** as well as tiles, since a filter bound to an explore is also a dependency to migrate.

## Attribution

thelook eCommerce LookML is licensed under the MIT License:

```
MIT License

Copyright (c) 2023 Looker Data Sciences, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
