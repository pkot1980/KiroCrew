const { test } = require("node:test");
const assert = require("node:assert");
const {
  _inRect,
  isPetWindowOpen,
  closePetWindow,
  hidePetWindow,
  showPetWindow,
  POLL_MS,
} = require("../petOverlays");

// The pet overlay covers an ENTIRE display and is click-through by default.
// Whether a click reaches the desktop below or the pet is decided purely by
// _inRect against the renderer-reported hitbox, so these bounds are the whole
// contract: get them wrong and the user either cannot click the pet, or cannot
// click anything else on their screen.

test("_inRect accepts points inside the rectangle", () => {
  const r = { x: 10, y: 20, w: 100, h: 50 };
  assert.equal(_inRect(50, 40, r), true);
  assert.equal(_inRect(10, 20, r), true, "top-left corner is inside");
  assert.equal(_inRect(110, 70, r), true, "bottom-right corner is inside");
});

test("_inRect rejects points outside the rectangle", () => {
  const r = { x: 10, y: 20, w: 100, h: 50 };
  assert.equal(_inRect(9, 40, r), false, "one px left");
  assert.equal(_inRect(111, 40, r), false, "one px right");
  assert.equal(_inRect(50, 19, r), false, "one px above");
  assert.equal(_inRect(50, 71, r), false, "one px below");
});

test("_inRect treats a null hitbox as a miss", () => {
  // This is the safety net: no known hitbox must mean "let the click through",
  // never "capture everything".
  assert.equal(_inRect(0, 0, null), false);
  assert.equal(_inRect(500, 500, null), false);
});

test("_inRect handles a zero-size rectangle", () => {
  // A pet mid-fade can report w/h 0; only the exact point may match, and it
  // must not throw or match broadly.
  const r = { x: 5, y: 5, w: 0, h: 0 };
  assert.equal(_inRect(5, 5, r), true);
  assert.equal(_inRect(6, 5, r), false);
});

test("no pet window is open before one is created", () => {
  assert.equal(isPetWindowOpen(), false);
});

test("closePetWindow is safe when nothing was opened", () => {
  // before-quit calls this unconditionally.
  assert.doesNotThrow(() => closePetWindow());
  assert.equal(isPetWindowOpen(), false);
});

test("hideAll primitives are safe no-ops when no pet exists", () => {
  // The hideAll hotkey (main.js mochiToggleHideAll) calls these unconditionally;
  // with no pet window they must not throw. hidePetWindow reports 'was not
  // visible' so the caller does not try to restore a window that never existed.
  assert.doesNotThrow(() => showPetWindow());
  assert.strictEqual(hidePetWindow(), false);
});

test("the cursor poll runs at ~60fps", () => {
  // Slower than this and the pet visibly swallows or drops clicks at its edge
  // while the cursor moves; the original settled on 16ms.
  assert.equal(POLL_MS, 16);
});

// The context menu is drawn INSIDE the click-through overlay, so it needs its
// own hitbox or every click on a row is forwarded to whatever sits behind the
// pet. This was the P0: the renderer reported a menu rect that nothing consumed.
const { _shouldIgnoreAt } = require("../petOverlays");

test("a point inside the open menu is NOT ignored", () => {
  const boxes = {
    pet: { x: 0, y: 0, w: 100, h: 100 },
    bubble: null,
    menu: { x: 200, y: 200, w: 160, h: 120 },
  };
  assert.equal(_shouldIgnoreAt(250, 250, boxes), false, "menu row must receive the click");
  assert.equal(_shouldIgnoreAt(50, 50, boxes), false, "pet still receives clicks");
  assert.equal(_shouldIgnoreAt(500, 500, boxes), true, "empty desktop still clicks through");
});

test("with no menu open the decision is pet/bubble only", () => {
  const boxes = { pet: { x: 0, y: 0, w: 10, h: 10 }, bubble: null, menu: null };
  assert.equal(_shouldIgnoreAt(5, 5, boxes), false);
  assert.equal(_shouldIgnoreAt(50, 50, boxes), true);
});

// ── Drag clamp geometry ───────────────────────────────────────────────────
// The pet may hang HALF off the left/right edge (that is how edge-peek reads as
// tucking behind the screen border) but never off the top or bottom. The
// hand-written predecessor used PET_W/PET_H = 120 here while the renderer's
// shared constants say 128, so every clamp was 8px off — a discrepancy nothing
// would report at runtime.
const { _clampLocal, PET_W, PET_H } = require("../petOverlays");

test("pet box matches the renderer's shared constants", () => {
  assert.strictEqual(PET_W, 128);
  assert.strictEqual(PET_H, 128);
});

test("clamp allows half the pet past the left and right edges", () => {
  const bounds = { width: 1000, height: 800 };
  assert.strictEqual(_clampLocal(-500, 100, bounds).x, -PET_W / 2);
  assert.strictEqual(_clampLocal(5000, 100, bounds).x, 1000 - PET_W / 2);
});

test("clamp keeps the pet fully on screen vertically", () => {
  const bounds = { width: 1000, height: 800 };
  assert.strictEqual(_clampLocal(0, -300, bounds).y, 0);
  // Bottom limit leaves the whole sprite visible, unlike the horizontal case.
  assert.strictEqual(_clampLocal(0, 5000, bounds).y, 800 - PET_H);
});

test("a position already inside the display is untouched", () => {
  const r = _clampLocal(400, 300, { width: 1000, height: 800 });
  assert.deepStrictEqual(r, { x: 400, y: 300 });
});

// Regression: a pet-instance switch registers a replacement overlay for the
// same display before the old window's async `closed` fires. The cleanup must
// be identity-checked or it evicts the live replacement, leaking an
// unreachable always-on-top full-screen window.
test("a stale closed handler does not evict the replacement overlay", () => {
  const { _registerOverlay, _getOverlays } = require("../petOverlays");
  const mk = () => {
    let closedCb = null;
    return { on: (ev, cb) => { if (ev === "closed") closedCb = cb; }, fireClosed: () => closedCb && closedCb() };
  };
  const DID = 99123; // unlikely to collide with other tests' display ids
  const oldWin = mk();
  const newWin = mk();
  _registerOverlay(DID, oldWin);
  _registerOverlay(DID, newWin); // replacement for the same display
  oldWin.fireClosed(); // old window's async close fires AFTER the swap
  try {
    assert.equal(_getOverlays().get(DID), newWin, "replacement must survive a stale close");
  } finally {
    _getOverlays().delete(DID);
  }
});

// ── Overlay auth-failure recovery ──────────────────────────────────────────
// A pet overlay covers a whole frameless, click-through display, so a gateway
// token-required page (401/403) rendered in it would trap the user behind an
// uncloseable full-screen error page (the reported "403 after sleep" bug). A
// 401/403 is a COMPLETED navigation, so the only signal is the status code.
const {
  _isOverlayAuthFailure,
  _isOverlayErrorPage,
  _shouldReauthOverlay,
  _overlayReauthDelayMs,
  OVERLAY_REAUTH_MAX_RETRIES,
} = require("../petOverlays");

test("only 401/403 count as an auth-failure navigation", () => {
  assert.equal(_isOverlayAuthFailure(401), true);
  assert.equal(_isOverlayAuthFailure(403), true);
  // A successful pet load and non-auth errors must NOT trigger a token re-mint,
  // which cannot fix them.
  assert.equal(_isOverlayAuthFailure(200), false);
  assert.equal(_isOverlayAuthFailure(304), false);
  assert.equal(_isOverlayAuthFailure(404), false);
  assert.equal(_isOverlayAuthFailure(500), false);
  assert.equal(_isOverlayAuthFailure(undefined), false);
});

test("hiding is decoupled from re-auth: ANY >=400 page is an error page", () => {
  // The reported harm is "an opaque page blankets the display" — a 404/500 body
  // is a completed navigation and traps the user identically, so the hide
  // decision must cover all of them, not only the auth statuses.
  assert.equal(_isOverlayErrorPage(400), true);
  assert.equal(_isOverlayErrorPage(401), true);
  assert.equal(_isOverlayErrorPage(403), true);
  assert.equal(_isOverlayErrorPage(404), true);
  assert.equal(_isOverlayErrorPage(500), true);
  assert.equal(_isOverlayErrorPage(503), true);
  // Success and redirects are NOT error pages.
  assert.equal(_isOverlayErrorPage(200), false);
  assert.equal(_isOverlayErrorPage(301), false);
  assert.equal(_isOverlayErrorPage(304), false);
  assert.equal(_isOverlayErrorPage(undefined), false);
  // A 500 must hide (error page) but NOT trigger a token re-mint (not auth).
  assert.equal(_isOverlayErrorPage(500) && !_isOverlayAuthFailure(500), true);
});

test("re-auth is attempted while retries remain and a provider is wired", () => {
  for (let attempt = 0; attempt < OVERLAY_REAUTH_MAX_RETRIES; attempt++) {
    assert.equal(_shouldReauthOverlay({ attempt, hasProvider: true }), true);
  }
});

test("re-auth stops once the retry budget is spent, so the overlay stays blank", () => {
  // Blank is safe (the pet is briefly absent); an endless reload loop or a shown
  // error page is not.
  assert.equal(_shouldReauthOverlay({ attempt: OVERLAY_REAUTH_MAX_RETRIES, hasProvider: true }), false);
});

test("re-auth never runs without a token provider", () => {
  // With no way to re-mint, retrying can only re-fetch the same 403 forever;
  // the overlay must just stay hidden.
  assert.equal(_shouldReauthOverlay({ attempt: 0, hasProvider: false }), false);
});

test("re-auth backoff grows exponentially and caps at 2s", () => {
  assert.equal(_overlayReauthDelayMs(0), 500);
  assert.equal(_overlayReauthDelayMs(1), 1000);
  assert.equal(_overlayReauthDelayMs(2), 2000);
  assert.equal(_overlayReauthDelayMs(3), 2000, "capped, never unbounded");
});

// Source guard: the fix only works if did-navigate drives the recovery, hides on
// ANY error page, and the load-finished handler refuses to reveal a blanked
// overlay. Electron wiring cannot be imported here, so assert the real source.
const fs = require("node:fs");
const path = require("node:path");
test("createOverlayForDisplay hides on any error page and re-auths only on 401/403", () => {
  const src = fs.readFileSync(path.join(__dirname, "..", "petOverlays.js"), "utf8");
  assert.match(src, /did-navigate/, "must hook did-navigate (an error page is not a did-fail-load)");
  // Hide is gated on the general error-page test, not on the auth test.
  assert.match(src, /isOverlayErrorPage\(httpResponseCode\)[\s\S]{0,200}win\.hide\(\)/,
    "an error page (>=400) must hide the overlay");
  assert.match(src, /isOverlayAuthFailure\(httpResponseCode\)[\s\S]{0,60}fastReauthOverlay/,
    "only an auth failure kicks off the token re-mint");
  assert.match(
    src,
    /!overlayBlanked\.has\(win\)[\s\S]{0,120}showInactive/,
    "the load-finished handler must refuse to reveal a blanked overlay",
  );
});

test("re-arm is driven by the reconcile tick, and the provider is wired once", () => {
  // Fable finding 2/3: recovery must not be one-shot, and the provider must not
  // be re-registered on the 5s reconcile hot path.
  const overlaySrc = fs.readFileSync(path.join(__dirname, "..", "petOverlays.js"), "utf8");
  assert.match(overlaySrc, /function rearmBlankedOverlays\(\)/, "petOverlays must expose the re-entry path");
  const idxSrc = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");
  assert.match(idxSrc, /rearmBlankedOverlays\(\)/, "reconcile must call the re-entry path each tick");
  // Registered exactly once, inside initMochi (defined near EOF, after
  // reconcileMochi), NOT on the 5s reconcile hot path.
  const initAt = idxSrc.indexOf("function initMochi");
  const provCount = (idxSrc.match(/setPetReauthProvider\(/g) || []).length;
  const provAt = idxSrc.indexOf("setPetReauthProvider(");
  assert.equal(provCount, 1, "the provider must be registered exactly once");
  assert.ok(initAt >= 0 && provAt > initAt,
    "setPetReauthProvider must be registered inside initMochi, not per reconcile tick");
});

test("re-auth re-resolves the target and never hands a remote overlay the local token", () => {
  // Regression guard for the credential-exposure / permanently-blank bug: the
  // provider must re-resolve the CURRENT target for its OWN token, and clear the
  // local token cache only for self — never blindly mint the local token and
  // send it to whatever origin the overlay is showing.
  const idxSrc = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");
  const at = idxSrc.indexOf("setPetReauthProvider(");
  assert.ok(at >= 0, "the reauth provider must be registered");
  const region = idxSrc.slice(at, at + 900);
  assert.match(region, /resolveMochiTarget/, "must re-resolve the current target for its own token");
  assert.match(region, /SELF_INSTANCE/, "must distinguish self from remote before clearing the local cache");
  // The provider must REFRESH only, never SWITCH: bail to reconcile when the
  // resolved target differs from what the overlay currently shows.
  assert.match(region, /target\.instanceId !== mochiPetInstanceId \|\| target\.baseUrl !== mochiPetBaseUrl[\s\S]{0,40}return null/,
    "re-auth must not switch targets behind reconcile's back");
});

// A superseded overlay (torn down by a concurrent instance switch mid-backoff)
// must not overwrite the shared target globals or reload itself, or later
// displays load the stale instance. isRegisteredOverlay is that liveness gate.
test("isRegisteredOverlay tracks whether a window is still in the registry", () => {
  const { _isRegisteredOverlay, _registerOverlay, _getOverlays } = require("../petOverlays");
  const DID = 99231;
  const win = { on() {} };
  assert.equal(_isRegisteredOverlay(win), false, "unregistered window is not live");
  _registerOverlay(DID, win);
  try {
    assert.equal(_isRegisteredOverlay(win), true, "registered window is live");
    _getOverlays().delete(DID); // simulate teardown by an instance switch
    assert.equal(_isRegisteredOverlay(win), false, "a torn-down window is no longer live");
  } finally {
    _getOverlays().delete(DID);
  }
});

test("re-auth reload refuses a superseded window and every reveal path honors the latch", () => {
  const overlaySrc = fs.readFileSync(path.join(__dirname, "..", "petOverlays.js"), "utf8");
  // The async reload must re-check the window is still registered AFTER the
  // await, before touching the shared globals (GPT 5.6 blocking race).
  assert.match(
    overlaySrc,
    /!isRegisteredOverlay\(win\)[\s\S]{0,40}!resolved[\s\S]{0,40}return;/,
    "reload must drop a superseded window before overwriting currentBaseUrl/currentToken",
  );
  // EVERY win.showInactive() reveal is guarded by the blanked latch, so no path
  // (handshake, hide-all restore, display transfer) can re-reveal an error page.
  const reveals = overlaySrc.match(/win\.showInactive\(\)/g) || [];
  const guarded = overlaySrc.match(/!overlayBlanked\.has\(win\)[\s\S]{0,80}win\.showInactive\(\)/g) || [];
  assert.ok(reveals.length >= 3, "expected the three overlay reveal sites");
  assert.equal(guarded.length, reveals.length, "every showInactive reveal must be latch-guarded");
});
