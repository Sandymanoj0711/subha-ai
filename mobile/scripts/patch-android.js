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
