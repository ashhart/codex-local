'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// The renderer sees one snapshot object and can only ask for actions; every
// file read and child process stays in the main process.
contextBridge.exposeInMainWorld('codexLocal', {
  onState: (callback) => ipcRenderer.on('state', (_event, snapshot) => callback(snapshot)),
  ready: () => ipcRenderer.invoke('ready'),
  launch: (selection) => ipcRenderer.invoke('launch', selection),
  endSession: () => ipcRenderer.invoke('end-session'),
  dismissSession: () => ipcRenderer.invoke('dismiss-session'),
  quitAndRetry: () => ipcRenderer.invoke('quit-and-retry'),
  restartModel: () => ipcRenderer.invoke('restart-model'),
  unloadModel: () => ipcRenderer.invoke('unload-model'),
  openDiagnostics: () => ipcRenderer.invoke('open-diagnostics'),
  reloadModels: () => ipcRenderer.invoke('reload-models'),
  chooseProject: () => ipcRenderer.invoke('choose-project'),
});
