// 栖语 Qiyu - Electron 桌面壳
// 主进程：拉起 Python 后端子进程 + 原生窗口 + 托盘，参考 ai-live2d 启动器结构
const { app, BrowserWindow, Tray, Menu, dialog, nativeImage } = require('electron')
const { spawn } = require('node:child_process')
const path = require('node:path')
const http = require('node:http')
const fs = require('node:fs')

const BACKEND_HOST = '127.0.0.1'
const BACKEND_PORT = Number(process.env.QIYU_PORT || 8765)
const APP_TITLE = '栖语 Qiyu'

let mainWindow = null
let tray = null
let backendProc = null
let quitting = false

function log(msg) {
  try {
    const dir = path.join(app.getPath('userData'), 'logs')
    fs.mkdirSync(dir, { recursive: true })
    fs.appendFileSync(path.join(dir, 'desktop.log'),
      `[${new Date().toLocaleTimeString()}] ${msg}\n`)
  } catch (e) { /* ignore */ }
}

function backendDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend')
  }
  // 开发模式：后端就在项目根目录（launcher 的上一级）
  return path.resolve(__dirname, '..', '..')
}

function pythonCommand() {
  if (process.env.QIYU_PYTHON) return process.env.QIYU_PYTHON
  const candidates = process.platform === 'win32'
    ? ['python', 'py']
    : ['python3', 'python']
  return candidates[0]
}

function healthCheck(timeoutMs = 2000) {
  return new Promise((resolve) => {
    const req = http.get({ host: BACKEND_HOST, port: BACKEND_PORT, path: '/health', timeout: timeoutMs }, (res) => {
      let body = ''
      res.on('data', (c) => { body += c })
      res.on('end', () => { try { resolve(JSON.parse(body)) } catch { resolve(null) } })
    })
    req.on('error', () => resolve(null))
    req.on('timeout', () => { req.destroy(); resolve(null) })
  })
}

function stopBackend() {
  if (backendProc) {
    try { backendProc.kill() } catch (e) {}
    backendProc = null
  }
}

function startBackend() {
  stopBackend()
  const cwd = backendDir()
  const dataDir = path.join(app.getPath('userData'), 'data')
  const env = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    QIYU_HOST: BACKEND_HOST,
    QIYU_PORT: String(BACKEND_PORT),
    QIYU_DATA_DIR: dataDir,
    PYTHONIOENCODING: 'utf-8',
  }
  log(`backend dir: ${cwd}`)
  log(`data dir: ${dataDir}`)
  log(`cmd: ${pythonCommand()} demo.py (port ${BACKEND_PORT})`)
  const opts = {
    cwd,
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  }
  try {
    backendProc = spawn(pythonCommand(), ['demo.py'], opts)
  } catch (e) {
    log(`spawn failed: ${e.message}`)
    return false
  }
  backendProc.stdout.on('data', (d) => log('[backend] ' + d.toString().trimEnd()))
  backendProc.stderr.on('data', (d) => log('[backend-err] ' + d.toString().trimEnd()))
  backendProc.on('exit', (code) => {
    log(`backend exited: ${code}`)
    backendProc = null
  })
  return true
}

async function waitForBackend(maxWaitMs = 30000) {
  const start = Date.now()
  while (Date.now() - start < maxWaitMs) {
    const h = await healthCheck()
    if (h && h.status === 'ok') return h
    await new Promise((r) => setTimeout(r, 600))
  }
  return null
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1380,
    height: 900,
    minWidth: 1040,
    minHeight: 700,
    title: APP_TITLE,
    autoHideMenuBar: true,
    backgroundColor: '#f5f5f7',
    icon: path.join(__dirname, '..', 'assets', 'icon.png'),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  mainWindow.loadURL(`http://${BACKEND_HOST}:${BACKEND_PORT}/`)
  mainWindow.on('close', (e) => {
    if (!quitting) {
      e.preventDefault()
      mainWindow.hide()
    }
  })
  mainWindow.on('closed', () => { mainWindow = null })
}

function createTray() {
  const iconPath = path.join(__dirname, '..', 'assets', 'icon.png')
  let icon
  try {
    icon = nativeImage.createFromPath(iconPath)
    if (process.platform === 'win32' && icon.isEmpty()) {
      const ico = path.join(__dirname, '..', 'assets', 'icon.ico')
      icon = nativeImage.createFromPath(ico)
    }
  } catch (e) { icon = nativeImage.createEmpty() }
  tray = new Tray(icon.resize({ width: 16, height: 16 }))
  tray.setToolTip('栖语 Qiyu')
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '显示主窗口', click: () => { if (mainWindow) { mainWindow.show(); mainWindow.focus() } } },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit() } },
  ]))
  tray.on('click', () => { if (mainWindow) { mainWindow.show(); mainWindow.focus() } })
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow) { mainWindow.show(); mainWindow.focus() }
  })

  app.whenReady().then(async () => {
    const started = startBackend()
    const health = await waitForBackend()
    if (!started || !health) {
      dialog.showErrorBox('栖语 Qiyu 启动失败',
        '后端服务未能启动。请确认本机已安装 Python 3.12+，且 127.0.0.1:' + BACKEND_PORT + ' 未被占用。\n详细日志：' +
        path.join(app.getPath('userData'), 'logs', 'desktop.log'))
      stopBackend()
      app.quit()
      return
    }
    log(`backend ready: ${health.mode} / ${health.characters} chars`)
    createWindow()
    createTray()

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
      else if (mainWindow) { mainWindow.show() }
    })
  })

  app.on('before-quit', () => { quitting = true })
  app.on('will-quit', () => { stopBackend() })
  app.on('window-all-closed', () => { /* 驻留托盘，不退出 */ })
}
