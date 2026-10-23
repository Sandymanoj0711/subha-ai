// After "cap add ios": add the microphone + speech permission texts Apple requires.
const fs = require('fs');
const f = 'ios/App/App/Info.plist';
let x = fs.readFileSync(f, 'utf8');
function add(key, text) {
  if (x.includes('<key>' + key + '</key>')) return;
  const i = x.lastIndexOf('</dict>');
  x = x.slice(0, i) + '\t<key>' + key + '</key>\n\t<string>' + text + '</string>\n' + x.slice(i);
}
add('NSMicrophoneUsageDescription', 'Subha AI uses the microphone so you can talk to Subha by voice.');
add('NSSpeechRecognitionUsageDescription', 'Subha AI converts your speech to text so Subha can reply.');
fs.writeFileSync(f, x);
console.log('Info.plist patched');
