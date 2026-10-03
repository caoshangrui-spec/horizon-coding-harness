# Third-party notices

The MIT license in [`LICENSE`](LICENSE) covers the original Horizon Coding Harness code and
documentation. Dependency-reduced benchmark fixtures retain the terms of their upstream
projects and are included only for offline, source-bound regression evaluation.

| Fixture | Upstream revision | License | Provenance |
|---|---|---|---|
| `cookiecutter-1-utf8-context` | `c15633745df6abdb24e02746b82aadb20b8cdf8c` | BSD-3-Clause | [Cookiecutter repository and license](https://github.com/cookiecutter/cookiecutter/blob/c15633745df6abdb24e02746b82aadb20b8cdf8c/LICENSE) |
| `fastapi-5-nested-field-clone` | `7cea84b74ca3106a7f861b774e9d215e5228728f` | MIT | [FastAPI repository and license](https://github.com/fastapi/fastapi/blob/7cea84b74ca3106a7f861b774e9d215e5228728f/LICENSE) |
| `tqdm-1-tenumerate-start` | `8cc777fe8401a05d07f2c97e65d15e4460feab88` | MPL-2.0 | [tqdm repository and license](https://github.com/tqdm/tqdm/blob/8cc777fe8401a05d07f2c97e65d15e4460feab88/LICENCE) |

The exact source and fixed commits, upstream tests, fix links, SPDX identifiers, and reduction
notes are machine-readable in
[`benchmarks/run_ab/bugsinpy-reduced-v1.yaml`](benchmarks/run_ab/bugsinpy-reduced-v1.yaml).

Full upstream checkouts under `benchmarks/run_ab/full/**/fixture/` are intentionally excluded
from this repository. They are prepared locally at the commits recorded in the manifests and
remain governed by their upstream licenses.
