'use strict';

// Optional CommonJS preload for apps whose dotenv loader overwrites launcher values.
// Only explicitly configured keys are restored. This does not intercept later direct
// process.env assignments or asynchronous/ESM configuration loaders.
const keys = (process.env.PJ_KEEP_ENV_KEYS || '').split(',').filter(Boolean);
const kept = Object.fromEntries(keys.filter(key => process.env[key] !== undefined)
  .map(key => [key, process.env[key]]));
if (keys.length) {
  const Module = require('module');
  const originalLoad = Module._load;
  Module._load = function () {
    try {
      return originalLoad.apply(this, arguments);
    } finally {
      for (const [key, value] of Object.entries(kept)) process.env[key] = value;
    }
  };
}
