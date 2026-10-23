// Writes www/config.js from environment variables (GitHub secrets). Never commit the output.
const fs = require('fs');
const url = (process.env.SUBHA_APP_URL || '').trim().replace(/\/$/, '');
const tok = (process.env.SUBHA_ACCESS_TOKEN || '').trim();
if (!url) { console.error('SUBHA_APP_URL secret is missing'); process.exit(1); }
fs.writeFileSync('www/config.js',
  'window.SUBHA_BASE=' + JSON.stringify(url) + ';\nwindow.SUBHA_TOKEN=' + JSON.stringify(tok) + ';\n');
console.log('config.js written for ' + url + (tok ? ' (token set)' : ' (no token)'));
