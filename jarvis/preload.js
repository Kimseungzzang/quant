const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('api', {
  baseUrl: 'http://127.0.0.1:8000',
});
