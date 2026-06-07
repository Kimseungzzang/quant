import * as THREE from '../../node_modules/three/build/three.module.js';

const ACCENT = new THREE.Color(0x00d4ff);
const IDLE_COLOR = new THREE.Color(0x0066aa);
const THINK_COLOR = new THREE.Color(0x00d4ff);
const SPEAK_COLOR = new THREE.Color(0x00ff99);

export class JarvisSphere {
  constructor(canvas) {
    this.canvas = canvas;
    this.state = 'idle'; // idle | thinking | speaking
    this._rings = [];
    this._ringTimer = 0;
    this._clock = new THREE.Clock();
    this._init();
    this._animate();
  }

  _init() {
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;

    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    this.renderer.setSize(w, h);
    this.renderer.setClearColor(0x000000, 0);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 100);
    this.camera.position.z = 3.5;

    // ── 내부 글로우 구체
    const innerGeo = new THREE.SphereGeometry(0.88, 32, 32);
    const innerMat = new THREE.MeshBasicMaterial({
      color: 0x001a33,
      transparent: true,
      opacity: 0.7,
    });
    this.innerSphere = new THREE.Mesh(innerGeo, innerMat);
    this.scene.add(this.innerSphere);

    // ── 와이어프레임 외구체
    const outerGeo = new THREE.SphereGeometry(1.0, 18, 18);
    const outerMat = new THREE.MeshBasicMaterial({
      color: IDLE_COLOR,
      wireframe: true,
      transparent: true,
      opacity: 0.55,
    });
    this.outerSphere = new THREE.Mesh(outerGeo, outerMat);
    this.scene.add(this.outerSphere);

    // ── 보조 와이어프레임 (반대 방향 회전)
    const outerGeo2 = new THREE.SphereGeometry(1.0, 10, 10);
    const outerMat2 = new THREE.MeshBasicMaterial({
      color: IDLE_COLOR,
      wireframe: true,
      transparent: true,
      opacity: 0.2,
    });
    this.outerSphere2 = new THREE.Mesh(outerGeo2, outerMat2);
    this.scene.add(this.outerSphere2);

    // ── 중심 발광 포인트
    const pointGeo = new THREE.SphereGeometry(0.08, 8, 8);
    const pointMat = new THREE.MeshBasicMaterial({ color: ACCENT });
    this.centerPoint = new THREE.Mesh(pointGeo, pointMat);
    this.scene.add(this.centerPoint);

    // ── 링 그룹
    this.ringGroup = new THREE.Group();
    this.scene.add(this.ringGroup);

    window.addEventListener('resize', () => this._onResize());
  }

  _createRing() {
    const geo = new THREE.RingGeometry(1.05, 1.08, 64);
    const mat = new THREE.MeshBasicMaterial({
      color: this.state === 'speaking' ? SPEAK_COLOR : ACCENT,
      transparent: true,
      opacity: 0.7,
      side: THREE.DoubleSide,
    });
    const ring = new THREE.Mesh(geo, mat);
    ring.rotation.x = Math.random() * Math.PI;
    ring.rotation.y = Math.random() * Math.PI;
    ring._scale = 1.0;
    ring._opacity = 0.7;
    this.ringGroup.add(ring);
    this._rings.push(ring);
  }

  _animate() {
    requestAnimationFrame(() => this._animate());
    const dt = this._clock.getDelta();
    const t = this._clock.getElapsedTime();

    // 구체 회전
    const speed = this.state === 'thinking' ? 1.8 : this.state === 'speaking' ? 2.5 : 0.4;
    this.outerSphere.rotation.y += 0.003 * speed;
    this.outerSphere.rotation.x += 0.001 * speed;
    this.outerSphere2.rotation.y -= 0.002 * speed;
    this.outerSphere2.rotation.z += 0.0015 * speed;

    // 색상 전환
    const targetColor = this.state === 'thinking' ? THINK_COLOR : this.state === 'speaking' ? SPEAK_COLOR : IDLE_COLOR;
    this.outerSphere.material.color.lerp(targetColor, 0.05);
    this.outerSphere2.material.color.lerp(targetColor, 0.05);

    // 내부 구체 펄스
    const pulse = 1 + Math.sin(t * (this.state === 'thinking' ? 4 : this.state === 'speaking' ? 6 : 1.5)) * 0.03;
    this.innerSphere.scale.setScalar(pulse);

    // 링 생성
    const ringInterval = this.state === 'thinking' ? 0.5 : this.state === 'speaking' ? 0.3 : 2.5;
    this._ringTimer += dt;
    if (this._ringTimer > ringInterval) {
      this._createRing();
      this._ringTimer = 0;
    }

    // 링 애니메이션
    for (let i = this._rings.length - 1; i >= 0; i--) {
      const ring = this._rings[i];
      ring._scale += dt * 1.2;
      ring._opacity -= dt * 0.6;
      ring.scale.setScalar(ring._scale);
      ring.material.opacity = Math.max(0, ring._opacity);
      if (ring._opacity <= 0) {
        this.ringGroup.remove(ring);
        this._rings.splice(i, 1);
      }
    }

    this.renderer.render(this.scene, this.camera);
  }

  setState(state) {
    this.state = state; // 'idle' | 'thinking' | 'speaking'
  }

  _onResize() {
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }
}
