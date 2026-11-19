// SVGO config for the icon set. build.mjs runs it over svg/*.svg in place.
// Shapes stay shapes (no convertShapeToPath) so a circle in the source is a
// circle in icons.ts and in the diff; children lose every presentation
// attribute because the root carries them all (lint.mjs enforces the same).
export default {
  multipass: true,
  js2svg: { pretty: false },
  plugins: [
    {
      name: 'preset-default',
      params: {
        overrides: {
          removeViewBox: false,
          convertShapeToPath: false,
          mergePaths: false,
          convertColors: { currentColor: true },
          removeUnknownsAndDefaults: { keepRoleAttr: true },
        },
      },
    },
    'sortAttrs',
    {
      name: 'removeAttrs',
      params: {
        // children only: the root keeps its stroke attributes
        attrs: ['(path|circle|rect|line|polyline|polygon|g):(fill|stroke|stroke-width|stroke-linecap|stroke-linejoin|class|style)'],
      },
    },
  ],
}
