# Apply the source overlay

Base: `jasethepeople/integration-gateway` at `acb4c399ad6ba19ee87ba01bb7bbd5b678591791`.

Copy the contents of `source/` into the root of that checkout, keeping paths. The archive contains every changed/new source file; it intentionally omits the unchanged registry and other baseline files. No runtime dependencies or lockfile entries were added.

The `evidence/` directory contains the test/build logs and loopback demo outputs. The demo binds only to `127.0.0.1` on `PORT=3456`; it does not deploy or expose a service.

To reproduce after applying the overlay: `npm ci --no-audit --no-fund`, `npm test -- --reporter=dot`, `npm run build`, then `PORT=3456 npm run demo:approval-gate`.
