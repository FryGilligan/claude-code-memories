// Build-time globals exposed as `{{ build.* }}` in templates.
// Used to bust the CSS cache so Cloudflare/browsers pick up new theme.css
// without manual purge.
module.exports = {
  timestamp: Date.now().toString(),
  year: new Date().getUTCFullYear(),
};
