/* Subha AI: native speech recognition for the Capacitor app.
   In the app, window.SpeechRecognition is replaced by a wrapper around the native plugin,
   so the existing chat code works unchanged. In a normal browser this file does nothing. */
(function () {
  var C = window.Capacitor;
  if (!C || !C.isNativePlatform || !C.isNativePlatform()) return;

  // Small on-screen message so problems can be seen on the phone (debug help).
  window.__toast = function (msg) {
    try {
      var d = document.getElementById('__toast');
      if (!d) {
        d = document.createElement('div'); d.id = '__toast';
        d.style.cssText = 'position:fixed;left:8px;right:8px;top:8px;z-index:99999;background:#222;color:#fff;font:13px sans-serif;padding:8px 10px;border-radius:8px;opacity:.95';
        (document.body || document.documentElement).appendChild(d);
      }
      d.textContent = String(msg); d.style.display = 'block';
      clearTimeout(d._t); d._t = setTimeout(function () { d.style.display = 'none'; }, 9000);
    } catch (e) {}
  };

  // Passcode + server URL come from config.js (written at build time from GitHub secrets).
  try { if (window.SUBHA_TOKEN) localStorage.setItem('subha_token', window.SUBHA_TOKEN); } catch (e) {}

  // Phone's own voice (free) - used when the server voice is down.
  var TTS = null;
  try { TTS = (C.Plugins && C.Plugins.TextToSpeech) || C.registerPlugin('TextToSpeech'); } catch (e) {}
  window.__nativeSpeak = function (text, done) {
    var finished = false;
    function end() { if (!finished) { finished = true; done(); } }
    if (!TTS) { window.__toast('Phone voice plugin missing'); end(); return; }
    var lang = /[\u0B80-\u0BFF]/.test(text) ? 'ta-IN' : 'en-IN';
    Promise.resolve(TTS.speak({ text: text, lang: lang, rate: 1.0, pitch: 1.0, volume: 1.0 }))
      .then(end)
      .catch(function (e) { window.__toast('Phone voice error: ' + String((e && e.message) || e)); end(); });
  };
  window.__nativeStop = function () { try { if (TTS) Promise.resolve(TTS.stop()).catch(function () {}); } catch (e) {} };

  // No bundler here, so get the native plugin through registerPlugin (works for native-only plugins).
  var Plugin = null;
  try { Plugin = (C.Plugins && C.Plugins.SpeechRecognition) || C.registerPlugin('SpeechRecognition'); } catch (e) {}
  if (!Plugin) {
    document.addEventListener('DOMContentLoaded', function () {
      var s = document.getElementById('status'); if (s) s.textContent = 'Mic plugin not found in this app build';
    });
    return;
  }

  // iOS: the Speech framework only supports some languages. If the chosen one is missing,
  // the plugin silently does nothing, so ask the phone what it supports and fall back.
  var IOS = false;
  try { IOS = !!(C.getPlatform && C.getPlatform() === 'ios'); } catch (e) {}
  var supported = null;
  function loadSupported() {
    if (!IOS || supported) return Promise.resolve(supported);
    return Promise.resolve().then(function () { return Plugin.getSupportedLanguages(); }).then(function (r) {
      supported = ((r && r.languages) || []).map(function (l) { return String(l).replace(/_/g, '-'); });
      return supported;
    }).catch(function () { supported = []; return supported; });
  }
  function pickLang(want) {
    if (!IOS || !supported || !supported.length) return want;
    var low = supported.map(function (l) { return l.toLowerCase(); });
    var w = want.toLowerCase(), i = low.indexOf(w);
    if (i >= 0) return supported[i];
    var base = w.split('-')[0];
    for (i = 0; i < low.length; i++) if (low[i].split('-')[0] === base) return supported[i];
    var fb = ['en-in', 'en-us', 'en-gb'];
    for (var k = 0; k < fb.length; k++) { i = low.indexOf(fb[k]); if (i >= 0) return supported[i]; }
    return want;
  }

  var SILENCE_MS = 1800;   // stop after this much quiet once the user has spoken
  var NO_SPEECH_MS = 8000; // give up if nothing is heard

  function NativeRec() {
    this.lang = 'en-IN';
    this.interimResults = true;
    this.continuous = false;
    this.maxAlternatives = 1;
    this._active = false;
  }

  NativeRec.prototype.start = function () {
    var self = this;
    if (self._active) throw new Error('already started');
    self._active = true; self._ended = false; self._last = ''; self._heard = false;
    self._handles = []; self._silence = null; self._noSpeech = null;

    function emit(text, isFinal) {
      if (!self.onresult) return;
      var r = [{ transcript: text }]; r.isFinal = isFinal;
      self.onresult({ resultIndex: 0, results: [r] });
    }
    function cleanup() {
      clearTimeout(self._silence); clearTimeout(self._noSpeech);
      self._handles.forEach(function (h) { try { if (h && h.remove) h.remove(); } catch (e) {} });
      self._handles = [];
    }
    function fail(code) {
      if (self.onerror) self.onerror({ error: code });
      finish();
    }
    function finish() {
      if (self._ended) return;
      self._ended = true; self._active = false;
      cleanup();
      if (self._last) emit(self._last, true);
      if (self.onend) self.onend();
    }
    self._finish = finish;
    self._stopNative = function () { try { Plugin.stop(); } catch (e) {} };

    function armSilence() {
      clearTimeout(self._silence);
      self._silence = setTimeout(function () { self._stopNative(); setTimeout(finish, 600); }, SILENCE_MS);
    }

    Plugin.requestPermissions().then(function (p) {
      if (self._ended) return;
      if (p.speechRecognition !== 'granted') { window.__toast('Mic/Speech permission is ' + p.speechRecognition + '. Open iPhone Settings > Subha AI and allow Microphone and Speech Recognition.'); fail('not-allowed'); return; }
      return loadSupported().then(function () { return Promise.resolve(Plugin.addListener('partialResults', function (d) {
        var t = (d && d.matches && d.matches[0]) || '';
        if (!t) return;
        clearTimeout(self._noSpeech);
        if (!self._heard) { self._heard = true; if (self.onspeechstart) self.onspeechstart(); }
        self._last = t; emit(t, false); armSilence();
      })); }).then(function (h) {
        self._handles.push(h);
        return Promise.resolve(Plugin.addListener('listeningState', function (d) {
          if (d && d.status === 'stopped') setTimeout(finish, 200);
        }));
      }).then(function (h) {
        self._handles.push(h);
        self._noSpeech = setTimeout(function () {
          if (!self._heard) { window.__toast('Mic is on but heard nothing'); self._stopNative(); fail('no-speech'); }
        }, NO_SPEECH_MS);
        if (self.onstart) self.onstart();
        var useLang = pickLang(self.lang);
        if (useLang !== self.lang) window.__toast('This iPhone cannot listen in ' + self.lang + '. Using ' + useLang + ' instead.');
        return Plugin.start({ language: useLang, maxResults: 1, partialResults: true, popup: false });
      });
    }).catch(function (e) {
      window.__toast('Mic error: ' + String((e && e.message) || e));
      var m = String((e && e.message) || e).toLowerCase();
      fail(m.indexOf('permission') >= 0 ? 'not-allowed' : m.indexOf('language') >= 0 ? 'language-not-supported' : 'audio-capture');
    });
  };

  NativeRec.prototype.stop = function () {
    if (!this._active) return;
    this._stopNative(); var f = this._finish; setTimeout(f, 500);
  };
  NativeRec.prototype.abort = function () {
    if (!this._active) return;
    this._last = ''; this._stopNative(); var f = this._finish; setTimeout(f, 100);
  };

  window.SpeechRecognition = NativeRec;
  window.webkitSpeechRecognition = NativeRec;
})();
