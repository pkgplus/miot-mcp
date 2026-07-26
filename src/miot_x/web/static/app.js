function app() {
    return {
        loggedIn: false, loginLoading: false, loginError: '', manualMode: false, callbackUrl: '', loginStep: 0, autoCallback: false, authUrl: '', codeDetected: false,
        tab: 'home', currentDevice: null, propValues: {},
        devices: [], scenes: [], homes: [], allHomes: [], selectedHomes: [],
        roomFilter: '', typeFilter: '', pollTimer: null,
        refreshing: false, xiaozhiConnected: false, showHomeGuide: false,
        autoRefreshTimer: null, homesSaving: false, homesSaved: false,

        get rooms() {
            const r = new Set(this.devices.map(d => d.room).filter(Boolean));
            return [...r];
        },
        get filteredDevices() {
            return this.devices.filter(d => !this.roomFilter || d.room === this.roomFilter);
        },
        get typedDevices() {
            if (!this.typeFilter) return this.devices;
            return this.devices.filter(d => this.getDeviceType(d) === this.typeFilter);
        },

        // ── URL 路由 ──────────────────────────────────
        routeFromURL() {
            const p = window.location.pathname;
            if (p === '/' || p === '/index.html') {
                this.tab = 'home'; this.currentDevice = null;
            } else if (p === '/devices') {
                this.tab = 'devices'; this.currentDevice = null;
            } else if (p === '/scenes') {
                this.tab = 'scenes'; this.currentDevice = null;
            } else if (p === '/settings') {
                this.tab = 'settings'; this.currentDevice = null;
            } else if (p.startsWith('/device/')) {
                const did = p.split('/device/')[1];
                if (did && this.devices.length > 0) {
                    const dev = this.devices.find(d => d.did === did);
                    if (dev) this.openDevice(dev, false);
                }
            }
        },
        pushURL(url) {
            if (window.location.pathname !== url) {
                history.pushState(null, '', url);
            }
        },

        async init() {
            await this.checkAuth();
            if (this.loggedIn) {
                await this.loadData();
                this.routeFromURL();
                this.checkXiaozhi();
                this.startAutoRefresh();
            }
            document.addEventListener('set-prop', e => {
                const { siid, piid, value } = e.detail;
                this.setPropValue(siid, piid, value);
            });
            // Listen for OAuth callback success from :443 window
            window.addEventListener('message', async (e) => {
                if (e.data && e.data.type === 'miot-x-login-success') {
                    if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
                    this.loginLoading = false;
                    await this.checkAuth();
                    if (this.loggedIn) { await this.loadData(); this.startAutoRefresh(); }
                }
            });
            // 浏览器前进/后退
            window.addEventListener('popstate', () => { this.routeFromURL(); });
        },

        startAutoRefresh() {
            if (this.autoRefreshTimer) clearInterval(this.autoRefreshTimer);
            this.autoRefreshTimer = setInterval(() => { this.loadDevices(); this.checkXiaozhi(); }, 30000);
        },

        async refreshDevices() {
            this.refreshing = true;
            try { await fetch('/api/devices?refresh=true'); await this.loadDevices(); } catch {}
            setTimeout(() => { this.refreshing = false; }, 600);
        },

        async checkXiaozhi() {
            try { const r = await fetch('/api/status'); const d = await r.json(); this.xiaozhiConnected = d.xiaozhi_connected || false; } catch {}
        },

        async saveHomesAndContinue() {
            await this.saveHomes();
            this.showHomeGuide = false;
            await this.loadDevices();
        },

        navigate(t, push) {
            if (push !== false) this.pushURL('/' + t);
            this.tab = t; this.currentDevice = null;
        },
        goBack() {
            if (history.length > 1) {
                history.back();
            } else {
                this.navigate('home');
            }
        },

        async checkAuth() {
            try { const r = await fetch('/api/auth/status'); const d = await r.json(); this.loggedIn = d.logged_in; } catch { this.loggedIn = false; }
        },
        async startLogin() {
            this.loginError = ''; this.loginStep = 1;
            try {
                const r = await fetch('/api/auth/start', { method: 'POST' }); const d = await r.json();
                this.authUrl = d.auth_url;
                this.autoCallback = d.auto_callback;
                if (d.auto_callback) {
                    window.open(d.auth_url, '_blank');
                    this.pollTimer = setInterval(async () => {
                        await this.checkAuth();
                        if (this.loggedIn) { clearInterval(this.pollTimer); await this.loadData(); this.startAutoRefresh(); }
                    }, 2000);
                    setTimeout(() => { if (!this.loggedIn && this.loginStep === 1) { clearInterval(this.pollTimer); this.loginStep = 2; } }, 60000);
                }
            } catch (e) { this.loginError = e.message; this.loginStep = 0; }
        },
        autoDetectCode() {
            this.codeDetected = /[?&]code=([^&]+)/.test(this.callbackUrl);
        },
        async submitCallback() {
            const m = this.callbackUrl.match(/[?&]code=([^&]+)/);
            if (!m) { this.loginError = '未能识别授权码，请确认粘贴了完整地址'; return; }
            this.loginError = '';
            try {
                const r = await fetch('/api/auth/callback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: m[1] }) });
                const d = await r.json();
                if (d.success) { this.loggedIn = true; this.loginStep = 0; await this.loadData(); this.startAutoRefresh(); }
                else { this.loginError = d.error || '登录失败，请重试'; }
            } catch (e) { this.loginError = e.message; }
        },
        async logout() { await fetch('/api/auth/logout', { method: 'POST' }); this.loggedIn = false; this.devices = []; this.scenes = []; this.homes = []; this.currentDevice = null; history.replaceState(null, '', '/'); },

        async loadData() {
            await Promise.all([this.loadDevices(), this.loadScenes(), this.loadHomes()]);
            if (this.homes.length > 0 && (!this.selectedHomes || this.selectedHomes.length === 0) && this.devices.length === 0) {
                this.showHomeGuide = true;
            }
        },
        async loadDevices() {
            try { const r = await fetch('/api/devices'); if (!r.ok) return; const d = await r.json(); this.devices = (d.devices || []).map(dev => ({ ...dev, _on: dev.power === true })); } catch {}
        },
        async loadScenes() { try { const r = await fetch('/api/scenes'); if (!r.ok) return; const d = await r.json(); this.scenes = d.scenes || []; } catch {} },
        async loadHomes() {
            try {
                const r = await fetch('/api/homes'); if (!r.ok) return;
                const d = await r.json();
                this.allHomes = d.homes || [];
                this.homes = d.homes || [];
                this.selectedHomes = d.selected || [];
            } catch {}
        },

        async openDevice(dev, push) {
            if (push !== false) this.pushURL('/device/' + dev.did);
            this.currentDevice = { ...dev, spec: null }; this.propValues = {};
            try { const r = await fetch(`/api/devices/${dev.did}`); if (!r.ok) return; const d = await r.json(); this.currentDevice = { ...d, _on: d.power === true }; await this.loadPropValues(); } catch {}
        },
        async loadPropValues() {
            if (!this.currentDevice?.spec) return;
            const promises = [];
            for (const svc of this.currentDevice.spec.services) {
                for (const prop of svc.properties) {
                    if (prop.access && prop.access.includes('read')) {
                        promises.push(
                            fetch(`/api/devices/${this.currentDevice.did}/prop/${svc.iid}/${prop.iid}`)
                                .then(r => r.ok ? r.json() : null)
                                .then(d => { if (d) this.propValues[`${svc.iid}-${prop.iid}`] = d.value; })
                                .catch(() => {})
                        );
                    }
                }
            }
            await Promise.all(promises);
        },

        async toggleProp(siid, piid) {
            const k = `${siid}-${piid}`; const nv = !this.propValues[k];
            await this.setPropValue(siid, piid, nv);
        },
        setDevicePower(dev, nextPower) {
            if (!dev) return;
            dev._on = nextPower;
            dev.power = nextPower;
            const listed = this.devices.find(d => d.did === dev.did);
            if (listed && listed !== dev) {
                listed._on = nextPower;
                listed.power = nextPower;
            }
        },
        async setPropValue(siid, piid, value) {
            if (!this.currentDevice) return;
            const k = `${siid}-${piid}`;
            const oldValue = this.propValues[k];
            const oldPower = this.currentDevice.power;
            const oldOn = this.currentDevice._on;
            this.propValues[k] = value;
            if (siid === 2 && piid === 1) this.setDevicePower(this.currentDevice, Boolean(value));
            if (siid === 2 && piid === 2) this.setDevicePower(this.currentDevice, true);
            try {
                const response = await fetch(`/api/devices/${this.currentDevice.did}/prop`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ siid, piid, value }) });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
            } catch {
                this.propValues[k] = oldValue;
                this.currentDevice._on = oldOn;
                this.currentDevice.power = oldPower;
                const listed = this.devices.find(d => d.did === this.currentDevice.did);
                if (listed) { listed._on = oldOn; listed.power = oldPower; }
            }
        },
        async quickToggle(dev) {
            const oldOn = dev._on;
            const oldPower = dev.power;
            const nextPower = !dev._on;
            this.setDevicePower(dev, nextPower);
            const action = nextPower ? 'on' : 'off';
            try {
                const response = await fetch(`/api/devices/${dev.did}/${action}`, { method: 'POST' });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
            } catch {
                dev._on = oldOn;
                dev.power = oldPower;
            }
        },
        async execAction(siid, aiid, inList) {
            if (!this.currentDevice) return;
            try { await fetch(`/api/devices/${this.currentDevice.did}/action`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ siid, aiid, in_list: inList || [] }) }); } catch {}
        },
        async runScene(id) { try { await fetch(`/api/scenes/${id}/run`, { method: 'POST' }); } catch {} },
        async saveHomes() {
            const ids = this.selectedHomes.length > 0 ? this.selectedHomes : null;
            await fetch('/api/homes/select', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ home_ids: ids }) });
            await this.loadDevices();
            await this.loadScenes();
        },
        async saveHomesWithFeedback() {
            this.homesSaving = true; this.homesSaved = false;
            await this.saveHomes();
            this.homesSaving = false; this.homesSaved = true;
            setTimeout(() => { this.homesSaved = false; }, 2000);
        },
        confirmLogout() {
            if (confirm('确定要退出登录吗？')) this.logout();
        },

        // Device type detection
        hasSpec(siid, piid) {
            if (!this.currentDevice?.spec) return false;
            for (const svc of this.currentDevice.spec.services) {
                if (svc.iid === siid) {
                    for (const p of svc.properties) { if (p.iid === piid) return true; }
                }
            }
            return false;
        },
        async execTTS(text) {
            if (!text || !this.currentDevice) return;
            try { await fetch(`/api/devices/${this.currentDevice.did}/action`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ siid: 5, aiid: 3, in_list: [text] }) }); } catch {}
        },
        getDeviceType(dev) {
            if (!dev) return 'default';
            const m = (dev.model || '').toLowerCase();
            const u = (dev.urn || '').toLowerCase();
            if (m.includes('tv') || m.includes('miir.tv') || u.includes(':television:')) return 'tv';
            if (m.includes('fan') || u.includes(':fan:')) return 'fan';
            if (m.includes('vacuum') || u.includes(':vacuum:') || u.includes(':sweeper:')) return 'vacuum';
            if (m.includes('speaker') || m.includes('wifispeaker') || u.includes(':speaker:')) return 'speaker';
            if (m.includes('light') || m.includes('lamp') || m.includes('bhf_light') || u.includes(':light:') || u.includes(':bath-heater:')) return 'light';
            if (m.includes('heater') || m.includes('aircondition') || m.includes('acpartner') || u.includes(':heater:') || u.includes(':air-condition')) return 'climate';
            if (m.includes('sensor') || u.includes(':sensor:') || u.includes(':motion-sensor:') || u.includes(':magnet-sensor:')) return 'sensor';
            if (m.includes('ctrl_ln') || m.includes('switch') || m.includes('plug') || m.includes('outlet') || u.includes(':switch:') || u.includes(':outlet:')) return 'switch';
            if (m.includes('camera') || m.includes('cateye') || u.includes(':camera:') || u.includes(':video-doorbell:')) return 'camera';
            if (m.includes('lock') || u.includes(':lock:')) return 'lock';
            return 'default';
        },
        deviceColor(dev) {
            if (!dev || !dev.online) return 'tile-off';
            return 'tile-' + this.getDeviceType(dev);
        },
        deviceIcon(dev, size) {
            const s = size || 24;
            const type = this.getDeviceType(dev);
            const color = { tv:'#4A4A4A', fan:'#32BAC8', vacuum:'#6C7BFF', speaker:'#9B8FFF', light:'#F5A623', climate:'#E8704A', sensor:'#6C7BFF', switch:'#44C553', camera:'#32BAC8', lock:'#F5A623', default:'#999' }[type];
            const paths = {
                tv: `<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/>`,
                fan: `<path d="M12 12m-2 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0"/><path d="M12 2C9 2 7 4 7 6c0 3 5 4 5 6"/><path d="M12 22c3 0 5-2 5-4 0-3-5-4-5-6"/><path d="M2 12c0 3 2 5 4 5 3 0 4-5 6-5"/><path d="M22 12c0-3-2-5-4-5-3 0-4 5-6 5"/>`,
                vacuum: `<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><path d="M12 3v2"/><path d="M12 19v2"/>`,
                speaker: `<path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>`,
                light: `<circle cx="12" cy="9" r="4"/><path d="M9 16h6"/><path d="M10 20h4"/><path d="M9 13a5 5 0 0 1-1-3 5 5 0 0 1 10 0 5 5 0 0 1-1 3"/>`,
                climate: `<path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/>`,
                sensor: `<path d="M12 2v2"/><path d="M12 20v2"/><path d="M4.93 4.93l1.41 1.41"/><path d="M17.66 17.66l1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><circle cx="12" cy="12" r="5"/>`,
                switch: `<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="12" cy="12" r="3"/><path d="M12 9V6"/>`,
                camera: `<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>`,
                lock: `<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>`,
                default: `<rect x="5" y="2" width="14" height="20" rx="3"/><circle cx="12" cy="18" r="1"/>`
            };
            return `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${paths[type]}</svg>`;
        },
        deviceStatus(dev) {
            if (!dev) return '';
            if (!dev.online) return '离线';
            if (dev.source === 'third_party' && dev.power == null) return '状态未知';
            const type = this.getDeviceType(dev);
            if (type === 'sensor') return '监测中';
            if (type === 'camera') return '在线';
            if (type === 'tv') return '就绪';
            if (type === 'vacuum') return '待命';
            if (type === 'speaker') return '就绪';
            if (type === 'lock') return '已锁定';
            return dev._on ? '已开启' : '待命';
        },
        isToggleable(dev) {
            const t = this.getDeviceType(dev);
            return t === 'light' || t === 'fan' || t === 'climate' || t === 'switch' || t === 'default';
        },

        // ── 通道名称（来自米家 sub_devices） ──
        getChannelName(siid) {
            const sub = this.currentDevice?.sub_devices;
            if (sub) {
                const key = 's' + siid;
                if (sub[key]?.name) return sub[key].name;
            }
            return siid === 2 ? '左键' : siid === 3 ? '右键' : '通道 ' + siid;
        },
    };
}
