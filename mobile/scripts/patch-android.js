// After "cap add android": allow the speech recognition service (needed on Android 11+).
const fs = require('fs');
const f = 'android/app/src/main/AndroidManifest.xml';
let x = fs.readFileSync(f, 'utf8');
if (!x.includes('android.speech.RecognitionService')) {
  x = x.replace('<application',
    '<queries>\n        <intent><action android:name="android.speech.RecognitionService"/></intent>\n    </queries>\n\n    <application');
}
if (!x.includes('android.permission.RECORD_AUDIO')) {
  x = x.replace('</manifest>', '    <uses-permission android:name="android.permission.RECORD_AUDIO"/>\n</manifest>');
}
fs.writeFileSync(f, x);
console.log('AndroidManifest patched');

// Let the WebView play Subha's voice without a fresh tap (autoplay), by replacing MainActivity.
function findMain(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = dir + '/' + e.name;
    if (e.isDirectory()) { const r = findMain(p); if (r) return r; }
    else if (e.name === 'MainActivity.java') return p;
  }
  return null;
}
const main = findMain('android/app/src/main/java');
if (main) {
  const pkg = (fs.readFileSync(main, 'utf8').match(/^package\s+([\w.]+);/m) || [])[1];
  fs.writeFileSync(main, `package ${pkg};

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getBridge().getWebView().getSettings().setMediaPlaybackRequiresUserGesture(false);
    }
}
`);
  console.log('MainActivity patched: ' + main);
} else { console.log('MainActivity.java not found'); }
