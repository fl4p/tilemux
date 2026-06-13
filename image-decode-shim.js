// createImageBitmap robustness shim — MUST load before xterm-addon-image.
//
// The image addon decodes IIP/Sixel via createImageBitmap(blob, {resizeWidth,
// resizeHeight}). Some browser contexts reject createImageBitmap(Blob) with
// "The source image could not be decoded" even for valid PNG/WebP — notably
// headless/automation Chromium, and real browsers with GPU image-decode
// disabled — while HTMLImageElement.decode()/onload handle the same bytes fine.
// The addon only falls back to an <img> when createImageBitmap is *absent*, not
// when it's present-but-failing, so inline images silently never render there.
//
// We wrap the global: try native, and on failure (Blob/File sources only) fall
// back to an <img> rasterized onto a canvas — honoring resizeWidth/resizeHeight —
// then hand back a real ImageBitmap via createImageBitmap(canvas), which needs
// no codec and so succeeds where the Blob decode didn't. Non-Blob sources (e.g.
// Sixel ImageData, or the addon's own canvas) pass straight through to native.
//
// Loaded as its own <script> ahead of the addon (see build-term.sh) so the
// addon's eval-time/late-bound references to createImageBitmap resolve to this
// wrapper. Idempotent.
(function installImageDecodeFallback() {
  if (typeof window.createImageBitmap !== 'function') return;
  if (window.createImageBitmap.__claudeWrapped) return;
  var native = window.createImageBitmap.bind(window);
  function viaImg(blob, opts) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(blob);
      var img = new Image();
      img.onload = function () {
        var w = (opts && opts.resizeWidth) || img.naturalWidth || img.width;
        var h = (opts && opts.resizeHeight) || img.naturalHeight || img.height;
        var c = document.createElement('canvas');
        c.width = w; c.height = h;
        try { c.getContext('2d').drawImage(img, 0, 0, w, h); } catch (e) {}
        URL.revokeObjectURL(url);
        // canvas -> ImageBitmap is a pixel copy (no codec); if even that is
        // refused, hand back the canvas itself (a valid CanvasImageSource).
        native(c).then(resolve, function () { c.close = function () {}; resolve(c); });
      };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('image decode failed')); };
      img.src = url;
    });
  }
  var wrapped = function (source) {
    var rest = arguments;
    if (typeof Blob === 'undefined' || !(source instanceof Blob)) return native.apply(null, rest);
    var opts = rest.length > 1 ? rest[1] : null;
    return native.apply(null, rest).catch(function () { return viaImg(source, opts); });
  };
  wrapped.__claudeWrapped = true;
  window.createImageBitmap = wrapped;
})();
