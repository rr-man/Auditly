# Third-party notices

Auditly itself is released under the MIT License (see `LICENSE`). It runs on the Python 3 standard library
and has no package dependencies. The one piece of third-party code it ships is vendored under `static/`,
and two sets of artwork carry their own terms.

## Mermaid 11.4.1 — `static/mermaid.min.js`

Draws the Flow chart. Served from this server only, never fetched from a CDN.

- Project: https://github.com/mermaid-js/mermaid
- Licence: MIT — Copyright (c) 2014–2024 Knut Sveidqvist and the Mermaid contributors
  (https://github.com/mermaid-js/mermaid/blob/develop/LICENSE)
- The minified bundle embeds the notices of the libraries Mermaid itself depends on (each MIT or
  equivalently permissive); they are preserved inside the file as distributed.

```
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
following conditions: The above copyright notice and this permission notice shall be included in all copies
or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
```

## Artwork

- `static/coeo_light.svg`, `static/coeo_dark.svg` — the COEO logo, the company's own trademark artwork.
  Included so the white-label option can show it; the MIT grant above covers the code, not the right to
  present your work as COEO's.
- `static/auditly_*.svg` — the Auditly wordmark, mark and tab icons, made for this project and covered by
  its licence.

## Sample data

Everything under `fixtures/` (the sample call, transcript, scorecard, knowledge document and test tone) is
fictional and was written for this project. It names no real customer, agent or company.
