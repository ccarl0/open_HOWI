three.js r179.1 (MIT, see LICENSE), from the `three@0.179.1` npm package, vendored so
open_HOWI works without an internet connection.

- `three.module.min.js`, `three.core.min.js`: unmodified copies of `build/`.
- `OrbitControls.js`: `examples/jsm/controls/OrbitControls.js` with its `from 'three'`
  import changed to `from './three.module.min.js'` (no import map needed).
