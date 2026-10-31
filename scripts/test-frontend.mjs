// No package installation/build needed: node --test scripts/test-frontend.mjs
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { preferredLocation, rememberLocation, phillipFirst } from '../website/location-preference.js';

const cafes = [{ id: 'braddon' }, { id: 'phillip' }, { id: 'kingston' }];
const originalWindow = globalThis.window;
afterEach(() => { globalThis.window = originalWindow; });
function storage(saved) {
  const values = new Map(saved ? [['e82-location-v1', saved]] : []);
  globalThis.window = { localStorage: {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  } };
  return values;
}

test('new visitors start at Phillip without recording a fabricated choice', () => {
  const values = storage();
  assert.equal(preferredLocation(cafes), 'phillip');
  assert.equal(values.size, 0);
});
test('explicit café choice survives a new visit and takes priority over preview props', () => {
  storage();
  rememberLocation('kingston');
  assert.equal(preferredLocation(cafes, 'phillip'), 'kingston');
});
test('stale or malformed preferences fall back to a real café', () => {
  for (const saved of ['removed-cafe', '{broken-json}', '']) {
    storage(saved);
    assert.equal(preferredLocation(cafes), 'phillip');
  }
  assert.equal(preferredLocation([{ id: 'kingston' }]), 'kingston');
});
test('storage denied or unavailable never prevents ordering or booking', () => {
  globalThis.window = { get localStorage() { throw new Error('blocked'); } };
  assert.doesNotThrow(() => rememberLocation('braddon'));
  assert.equal(preferredLocation(cafes), 'phillip');
  globalThis.window = undefined;
  assert.equal(preferredLocation(cafes), 'phillip');
});
test('valid preview defaults work only without a remembered choice', () => {
  storage();
  assert.equal(preferredLocation(cafes, 'kingston'), 'kingston');
  assert.equal(preferredLocation(cafes, 'missing'), 'phillip');
});
test('Phillip leads every list while source and other café order stay intact', () => {
  const before = cafes.map(cafe => cafe.id);
  assert.deepEqual(phillipFirst(cafes).map(cafe => cafe.id), ['phillip', 'braddon', 'kingston']);
  assert.deepEqual(cafes.map(cafe => cafe.id), before);
});

const html = readFileSync(new URL('../website/index.html', import.meta.url), 'utf8');
const source = html.match(/<script type="text\/x-dc"[^>]*>([\s\S]*?)<\/script>/)[1];
test('the complete embedded customer app remains valid JavaScript', () => {
  assert.doesNotThrow(() => new vm.Script(source));
});
test('remove only the landing introduction; retain featured location and site animations', () => {
  assert.ok(!html.includes('home-phillip-title'));
  assert.ok(html.includes('{{ cafesView }}') && html.includes('{{ cafeDetailView }}'));
  for (const feature of ['data-cafe-track', 'data-reveal', 'bkSubmit', 'submitEnquiry', 'placeOrder']) {
    assert.ok(html.includes(feature), feature);
  }
});
test('booking payload still goes through the existing public API', async () => {
  let request;
  const context = vm.createContext({
    DCLogic: class {
      setState(patch, callback) {
        Object.assign(this.state, typeof patch === 'function' ? patch(this.state) : patch);
        callback?.();
      }
    },
    window: { innerWidth: 1440, scrollTo() {} },
  });
  const Component = new vm.Script(source + '\nComponent;').runInContext(context);
  const app = new Component();
  app.D = { LOCATIONS: cafes, postForm: async (path, body) => {
    request = { path, body }; return { ok: true, data: { reference: 'test-only-reference' } };
  } };
  app.state.bk = { loc: 'phillip', size: '3', date: '2026-11-10', time: '10:00',
    name: ' Test Guest ', phone: '0400000000', email: 'guest@example.com', seat: 'Indoor', notes: 'Coeliac' };
  app.bkSubmit();
  await Promise.resolve();
  assert.equal(request.path, '/api/v1/public/bookings');
  assert.equal(request.body.location, 'phillip');
  assert.equal(request.body.party_size, 3);
  assert.equal(request.body.name, 'Test Guest');
  assert.equal(request.body.notes, 'Coeliac');
  assert.equal(request.body.seating, 'indoor');
  assert.equal(app.state.bkRef, 'test-only-reference');
  assert.equal(app.state.bkSending, false);
});

test('a booking the API refuses shows its message instead of opening an email draft', async () => {
  let navigated = false;
  const context = vm.createContext({
    DCLogic: class {
      setState(patch, callback) {
        Object.assign(this.state, typeof patch === 'function' ? patch(this.state) : patch);
        callback?.();
      }
    },
    window: { innerWidth: 1440, scrollTo() {}, location: { set href(v) { navigated = true; } } },
  });
  const Component = new vm.Script(source + '\nComponent;').runInContext(context);
  const app = new Component();
  app.D = { LOCATIONS: cafes, SOURCE: 'api', postForm: async () => ({ ok: false,
    data: { error: { code: 'location_closed', message: 'Café is closed on this date.', fields: {} } } }) };
  app.state.bk = { loc: 'phillip', size: '2', date: '2026-11-15', time: '10:00',
    name: 'Guest', phone: '0400000000', email: 'guest@example.com', seat: 'Outdoor', notes: '' };
  app.bkSubmit();
  await Promise.resolve();
  assert.equal(app.state.bkApiErr, 'Café is closed on this date.');
  assert.equal(app.state.bkDone, false);
  assert.equal(app.state.bkSending, false);
  assert.equal(navigated, false);
});

function enquiryHarness(postForm) {
  let navigated = false;
  const context = vm.createContext({
    DCLogic: class {
      setState(patch, callback) {
        Object.assign(this.state, typeof patch === 'function' ? patch(this.state) : patch);
        callback?.();
      }
    },
    window: { innerWidth: 1440, scrollTo() {}, location: { set href(v) { navigated = true; } } },
  });
  const Component = new vm.Script(source + '\nComponent;').runInContext(context);
  const app = new Component();
  app.D = { LOCATIONS: cafes, CATERING: [{ id: 'Breakfast' }, { id: 'Lunch' }], SOURCE: 'api', postForm };
  app.state.ctab = 1;
  app.state.ct = { date: '2026-12-05' };
  const control = (label, value, tag = 'input') => ({ value, tagName: tag, closest: () => ({ querySelector: () => ({ textContent: label }) }), getAttribute: () => null });
  const controls = [control('First name', 'Test'), control('Last name', 'Caterer'), control('Email', 'cater@example.com'), control('Phone', '0400000000'),
    control('Company name (optional)', 'Test Co'), control('Occasion?', 'Office breakfast'), control('How many people?', '25'), control('Location?', 'Civic office'),
    control('Catering menu', 'Lunch', 'select'), control('Enquiry', 'Vegan options please', 'textarea')];
  const event = { preventDefault() {}, currentTarget: { querySelectorAll: () => controls, reportValidity: () => true } };
  return { app, event, navigated: () => navigated };
}

test('a catering enquiry sends every field to the API and succeeds only with a real reference', async () => {
  let request;
  const { app, event, navigated } = enquiryHarness(async (path, body) => { request = { path, body }; return { ok: true, data: { reference: 'EQ-TEST' } }; });
  app.submitEnquiry('catering')(event);
  await Promise.resolve();
  assert.equal(request.path, '/api/v1/public/enquiries');
  assert.equal(request.body.kind, 'catering');
  assert.equal(request.body.name, 'Test Caterer');
  assert.equal(request.body.email, 'cater@example.com');
  assert.equal(request.body.company, 'Test Co');
  assert.equal(request.body.event_date, '2026-12-05');
  assert.equal(request.body.headcount, 25);
  assert.equal(request.body.package, 'lunch');
  assert.equal(JSON.stringify(request.body.payload), JSON.stringify({ occasion: 'Office breakfast', eventLocation: 'Civic office' }));
  assert.ok(request.body.message.includes('Enquiry: Vegan options please'));
  assert.equal(app.state.enquiryRef, 'EQ-TEST');
  assert.equal(app.state.enquiryDraft, 'catering');
  assert.equal(navigated(), false);
});

test('a catering enquiry the API refuses shows the message and never opens an email draft', async () => {
  const { app, event, navigated } = enquiryHarness(async () => ({ ok: false, data: { error: { code: 'invalid', message: 'Please check the submitted fields.', fields: { event_date: 'Choose today or a future date.' } } } }));
  app.submitEnquiry('catering')(event);
  await Promise.resolve();
  assert.equal(app.state.enquiryError, 'Please check the submitted fields. event date: Choose today or a future date.');
  assert.equal(app.state.enquiryErrorKind, 'catering');
  assert.equal(app.state.enquiryRef, '');
  assert.equal(app.state.enquiryDraft, null);
  assert.equal(navigated(), false);
});

test('a café selected while bootstrap is loading is not overwritten by a saved default', async () => {
  let resolveData;
  let remembered;
  const context = vm.createContext({
    DCLogic: class { setState(patch, callback) {
      Object.assign(this.state, typeof patch === 'function' ? patch(this.state) : patch);
      callback?.();
    } },
    window: { innerWidth: 1440, scrollTo() {}, addEventListener() {} },
    location: { hash: '' },
    requestAnimationFrame() {}, setTimeout() {},
    loadData: () => new Promise(resolve => { resolveData = resolve; }),
    loadAccount: async () => ({ handleAccountLink: async () => '', restoreSession: async () => null }),
    loadCms: () => new Promise(() => {}),
    document: { title: '', querySelector: () => null },
  });
  const isolatedSource = source.replace(/import\(new URL\('account\.js[^\n]+?\)\.href\)/, 'loadAccount()')
    .replace(/import\(new URL\('cms\.js[^\n]+?\)\.href\)/, 'loadCms()')
    .replace(/import\(new URL\([^\n]+?\)\.href\)/, 'loadData()');
  const Component = new vm.Script(isolatedSource + '\nComponent;').runInContext(context);
  const app = new Component();
  app.hydrateImages = app.observeReveals = () => {};
  app.componentDidMount();
  app.go('reserve', { bk: { ...app.state.bk, loc: 'phillip' } })();
  resolveData({ LOCATIONS: cafes, preferredLocation: () => 'kingston',
    rememberLocation: id => { remembered = id; } });
  await Promise.resolve();
  assert.equal(app.state.bk.loc, 'phillip');
  assert.equal(app.state.locId, 'phillip');
  assert.equal(remembered, 'phillip');
});

// Customer accounts: website/account.js talks to /api/v1/auth and /api/v1/me (fetch is faked).
const saved = new Map();
globalThis.location = { hostname: 'site.example', pathname: '/', search: '', replace(url) { this.replaced = url; } };
globalThis.history = { replaceState(state, title, url) { location.pathname = url; location.search = ''; } };
globalThis.localStorage = { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value),
  removeItem: key => saved.delete(key) };
const account = await import('../website/account.js');
function fakeApi(routes) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    const path = url.replace(/^https?:\/\/[^/]+/, '');
    const call = { path, method: init.method, auth: init.headers.Authorization, body: init.body && JSON.parse(init.body) };
    calls.push(call);
    const [status, data] = routes[path](call, calls);
    return { ok: status < 400, status, json: async () => data };
  };
  return calls;
}
const tokens = n => ({ access: 'access-' + n, refresh: 'refresh-' + n });
const profile = { email: 'guest@example.com', first_name: 'Test', last_name: 'Guest', phone: '0400000000' };

test('signing in stores only the refresh token and loads the profile with the access token', async () => {
  saved.clear();
  const calls = fakeApi({ '/api/v1/auth/login': () => [200, tokens(1)], '/api/v1/me': () => [200, profile] });
  const res = await account.signIn('guest@example.com', 'correct-horse-1');
  assert.equal(res.ok, true);
  assert.equal(res.data.first_name, 'Test');
  assert.deepEqual(calls[0].body, { email: 'guest@example.com', password: 'correct-horse-1' });
  assert.equal(calls[1].auth, 'Bearer access-1');
  assert.equal(saved.get('e82-refresh-v1'), 'refresh-1');
});
test('a returning visitor is restored with one refresh even when asked twice at once', async () => {
  saved.clear(); saved.set('e82-refresh-v1', 'refresh-1');
  const calls = fakeApi({ '/api/v1/auth/refresh': () => [200, tokens(2)], '/api/v1/me': () => [200, profile] });
  const [a, b] = await Promise.all([account.restoreSession(), account.restoreSession()]);
  assert.equal(a.email, 'guest@example.com');
  assert.equal(b.email, 'guest@example.com');
  assert.equal(calls.filter(c => c.path === '/api/v1/auth/refresh').length, 1);
  assert.equal(saved.get('e82-refresh-v1'), 'refresh-2');
});
test('an expired session is forgotten; a network failure keeps it for next time', async () => {
  saved.clear(); saved.set('e82-refresh-v1', 'stale');
  fakeApi({ '/api/v1/auth/refresh': () => [401, { error: { code: 'token_not_valid' } }] });
  assert.equal(await account.restoreSession(), null);
  assert.equal(saved.has('e82-refresh-v1'), false);
  saved.set('e82-refresh-v1', 'still-good');
  globalThis.fetch = async () => { throw new TypeError('offline'); };
  assert.equal(await account.restoreSession(), null);
  assert.equal(saved.get('e82-refresh-v1'), 'still-good');
});
test('sign-up sends the honeypot and the API field errors reach the form', async () => {
  saved.clear();
  const calls = fakeApi({ '/api/v1/auth/register': () => [400, { error: { code: 'invalid', message: 'Please check the submitted fields.',
    fields: { email: ['This email is already registered.'], non_field_errors: ['This password is too common.'] } } }] });
  const res = await account.signUp({ email: 'guest@example.com', password: 'password12', first_name: 'Test', last_name: 'Guest', phone: '' });
  assert.equal(calls[0].body.website, '');
  const errors = account.errorsOf(res);
  assert.equal(errors.email, 'This email is already registered.');
  assert.equal(errors.non_field_errors, 'This password is too common.');
  assert.equal(account.errorsOf({ status: 401, data: null }).message, 'Email or password is incorrect.');
  assert.match(account.errorsOf({ status: 0, data: null }).message, /reach EightyTwenty/);
});
test('logging out revokes the refresh token on the server and forgets it', async () => {
  saved.clear();
  fakeApi({ '/api/v1/auth/login': () => [200, tokens(3)], '/api/v1/me': () => [200, profile] });
  await account.signIn('guest@example.com', 'correct-horse-1');
  const calls = fakeApi({ '/api/v1/auth/logout': () => [204, null] });
  await account.signOut();
  assert.deepEqual(calls[0].body, { refresh: 'refresh-3' });
  assert.equal(calls[0].auth, 'Bearer access-3');
  assert.equal(saved.has('e82-refresh-v1'), false);
});
test('email links: reset opens the API password page, verification is confirmed on the site', async () => {
  location.pathname = '/reset-password'; location.search = '?uid=MQ&token=abc';
  assert.equal(await account.handleAccountLink(), '');
  assert.equal(location.replaced, 'https://eightytwenty-api.onrender.com/account/reset-password?uid=MQ&token=abc');
  location.pathname = '/verify-email'; location.search = '?uid=MQ&token=abc';
  const calls = fakeApi({ '/api/v1/auth/verify-email': () => [200, { message: 'Email verified.' }] });
  assert.match(await account.handleAccountLink(), /verified/);
  assert.deepEqual(calls[0].body, { uid: 'MQ', token: 'abc' });
  assert.equal(location.pathname, '/');
  assert.equal(await account.handleAccountLink(), '');
});

function mountWithAccount(api) {
  const context = vm.createContext({
    DCLogic: class { setState(patch, callback) {
      Object.assign(this.state, typeof patch === 'function' ? patch(this.state) : patch);
      callback?.();
    } },
    window: { innerWidth: 1440, scrollTo() {} }, document: { querySelector: () => null },
  });
  const app = new (new vm.Script(source + '\nComponent;').runInContext(context))();
  app.A = api; app._account = Promise.resolve(api);
  return app;
}
const settle = () => new Promise(resolve => setImmediate(resolve));
test('the login form signs in through the account API and opens the real profile', async () => {
  const seen = [];
  const app = mountWithAccount({ signIn: async (email, password) => { seen.push([email, password]); return { ok: true, status: 200, data: profile }; } });
  app.state.au = { email: ' Guest@Example.com ', password: 'correct-horse-1' };
  app.auSubmit('login')({ preventDefault() {} });
  await settle();
  assert.deepEqual(seen, [['guest@example.com', 'correct-horse-1']]);
  assert.equal(app.state.signedIn, true);
  assert.deepEqual({ ...app.state.user }, { name: 'Test Guest', email: 'guest@example.com', phone: '0400000000' });
  assert.equal(app.state.view, 'account');
  assert.equal(app.state.auBusy, null);
});
test('a rejected login stays signed out and says why', async () => {
  const app = mountWithAccount({ signIn: async () => ({ ok: false, status: 401, data: null }),
    errorsOf: () => ({ message: 'Email or password is incorrect.' }) });
  app.state.au = { email: 'guest@example.com', password: 'wrong-password' };
  app.auSubmit('login')({ preventDefault() {} });
  await settle();
  assert.notEqual(app.state.signedIn, true);
  assert.equal(app.state.auSrv.login, 'Email or password is incorrect.');
  assert.equal(app.state.auBusy, null);
});
test('my bookings come from the API; a signed-in booking carries the token, a stale one books as guest', async () => {
  saved.clear();
  fakeApi({ '/api/v1/auth/login': () => [200, tokens(4)], '/api/v1/me': () => [200, profile] });
  await account.signIn('guest@example.com', 'correct-horse-1');
  let calls = fakeApi({ '/api/v1/me/bookings?page_size=20': () => [200, { count: 1, results: [{ reference: 'BK-1', location: 'phillip', status: 'confirmed' }] }],
    '/api/v1/public/bookings': () => [201, { reference: 'BK-2', status: 'requested' }] });
  assert.equal((await account.myBookings())[0].reference, 'BK-1');
  const booked = await account.bookAsCustomer({ location: 'phillip' });
  assert.equal(booked.data.reference, 'BK-2');
  assert.equal(calls.at(-1).auth, 'Bearer access-4');
  calls = fakeApi({ '/api/v1/public/bookings': call => call.auth ? [401, null] : [201, { reference: 'BK-3' }],
    '/api/v1/auth/refresh': () => [401, null] });
  assert.equal((await account.bookAsCustomer({ location: 'phillip' })).data.reference, 'BK-3');
  assert.equal(calls.at(-1).auth, undefined);
});
test('the reservations tab lists the account bookings from the API', async () => {
  const app = mountWithAccount({ myBookings: async () => [{ reference: 'BK-1', location: 'phillip', date: '2026-11-10', time: '09:30:00', party_size: 3, status: 'confirmed', notes: 'Table: Outdoor\nHigh chair' }] });
  app.D = { LOCATIONS: [{ id: 'phillip', name: 'Phillip', booking: 'Phillip - 100% Gluten Free' }] };
  app.loadMyBookings();
  await settle();
  const [row] = app.bookingRows();
  assert.equal(row.cafe, 'Phillip - 100% Gluten Free');
  assert.match(row.when, /9:30 am$/);
  assert.equal(row.size, 3);
  assert.equal(row.seat, ' · Outdoor');
  assert.equal(row.status, 'Confirmed');
});
// BOS CMS → website (website/cms.js + the component's CMS slots).
const cms = await import('../website/cms.js');
const cmsSettings = {
  contact_email: 'hello@example.com', footer: 'Fresh food daily.', announcement: 'Closed on Christmas Day',
  social_links: { Instagram: 'https://instagram.example/e82', facebook: 'javascript:alert(1)' },
  navigation: [
    { id: 'n1', menu: 'header', label: 'Our story', url: '/our-story', parent: null },
    { id: 'n2', menu: 'footer', label: 'Careers', url: 'https://jobs.example/e82', parent: null },
    { id: 'n3', menu: 'footer', label: 'Child', url: '/privacy', parent: 'n2' },
    { id: 'n4', menu: 'footer', label: 'Bad', url: '/../etc', parent: null },
    { id: 'n5', menu: 'header', label: 'Menu', url: '/menu', parent: null },
  ],
  pages: [{ slug: 'privacy', title: 'Privacy', seo_title: 'Privacy | E82', seo_description: 'How we use data' }],
};
test('CMS links route to site views, CMS pages or external URLs only', () => {
  assert.deepEqual(cms.route('/privacy'), { view: 'privacy' });
  assert.deepEqual(cms.route('/refunds'), { view: 'refund' });
  assert.deepEqual(cms.route('/bookings'), { view: 'reserve' });
  assert.deepEqual(cms.route('/our-story'), { view: 'page', slug: 'our-story' });
  assert.deepEqual(cms.route('https://jobs.example/e82'), { href: 'https://jobs.example/e82' });
  assert.equal(cms.route('javascript:alert(1)'), null);
  assert.deepEqual(cms.navItems(cmsSettings, 'footer').map(n => n.label), ['Careers']);
  assert.equal(cms.social(cmsSettings, 'instagram', 'x'), 'https://instagram.example/e82');
  assert.equal(cms.social(cmsSettings, 'facebook', 'fallback'), 'fallback');
  assert.deepEqual(cms.seo(cms.published(cmsSettings, 'privacy'), { title: 'T', description: 'D' }), { title: 'Privacy | E82', description: 'How we use data' });
  assert.deepEqual(cms.seo(null, { title: 'T', description: 'D' }), { title: 'T', description: 'D' });
});
test('pages are fetched once per slug and a missing page resolves null', async () => {
  const seen = [];
  globalThis.fetch = async url => { seen.push(url); return url.endsWith('/privacy') ? { ok: true, json: async () => ({ slug: 'privacy', title: 'Privacy', blocks: [] }) } : { ok: false }; };
  assert.equal((await cms.fetchPage('privacy')).title, 'Privacy');
  await cms.fetchPage('privacy');
  assert.equal(await cms.fetchPage('nope'), null);
  assert.deepEqual(seen.map(u => u.replace(/^https?:\/\/[^/]+/, '')), ['/api/v1/public/pages/privacy', '/api/v1/public/pages/nope']);
});
function mountWithCms(settings, page) {
  const context = vm.createContext({
    DCLogic: class { setState(patch, callback) {
      Object.assign(this.state, typeof patch === 'function' ? patch(this.state) : patch);
      callback?.();
    } },
    window: { innerWidth: 1440, scrollTo() {} }, document: { title: 'Default', querySelector: () => null },
    React: { createElement: (type, props, ...children) => ({ type, props, children: children.flat() }) },
  });
  const app = new (new vm.Script(source + '\nComponent;').runInContext(context))();
  app.D = { LOCATIONS: cafes, SETTINGS: settings };
  app.C = Object.assign({}, cms, { fetchPage: async () => page });
  app._seoDefault = { title: 'Default', description: '' };
  app.state.ready = true;
  return { app, context };
}
const text = node => node == null ? '' : typeof node === 'string' ? node : (node.children || []).map(text).join(' ');
test('BOS settings fill the announcement, footer, socials, contact and CMS navigation', () => {
  const { app } = mountWithCms(cmsSettings, null);
  const v = app.cmsVals('home');
  assert.deepEqual({ ...v.cmsAnnounce }, { show: true, text: 'Closed on Christmas Day' });
  assert.equal(v.cmsFooterOn, true);
  assert.equal(v.contactMailto, 'mailto:hello@example.com');
  assert.equal(v.socialInstagram, 'https://instagram.example/e82');
  assert.equal(v.socialFacebook, 'https://www.facebook.com/eightytwentyfood');
  assert.deepEqual(v.cmsHeaderNav.map(n => n.href), ['#our-story']); // built-in Menu link is not duplicated
  assert.equal(v.cmsFooterNav[0].target, '_blank');
  const empty = mountWithCms({}, null).app.cmsVals('home');
  assert.equal(empty.cmsAnnounce.show, false);
  assert.equal(empty.cmsFooterOff, true);
  assert.equal(empty.contactEmail, 'info@eightytwentyfood.com.au');
  assert.equal(empty.cmsHeaderNav.length, 0);
});
test('a published legal page replaces the built-in copy and sets SEO; others keep it', async () => {
  const page = { slug: 'privacy', title: 'Privacy', seo_title: 'Privacy | E82', seo_description: 'How we use data', blocks: [
    { key: 'intro', type: 'rich_text', data: { text: 'First paragraph.\n\nSecond paragraph.' } },
    { key: 'faq', type: 'faq', data: { items: [{ question: 'Do you sell data?', answer: 'No.' }] } },
    { key: 'go', type: 'cta', data: { label: 'Book a table', url: '/bookings' } },
  ] };
  const { app, context } = mountWithCms(cmsSettings, page);
  app.state.view = 'privacy';
  app.syncCms();
  await settle();
  assert.equal(context.document.title, 'Privacy | E82');
  const v = app.cmsVals('privacy');
  assert.equal(v.cmsLegalOn, true);
  const words = text(v.cmsArticle);
  for (const w of ['Privacy', 'First paragraph.', 'Second paragraph.', 'Do you sell data?', 'Book a table']) assert.ok(words.includes(w), w);
  app.state.view = 'refund';
  app.syncCms();
  await settle();
  assert.equal(app.state.cmsPage, null);
  assert.equal(app.cmsVals('refund').cmsLegalOff, true);
  assert.equal(context.document.title, 'Default');
});
test('a published legal page with no blocks keeps the built-in copy but still sets SEO', async () => {
  const { app, context } = mountWithCms(cmsSettings, { slug: 'privacy', title: 'Privacy', seo_title: 'Privacy | E82', seo_description: '', blocks: [] });
  app.state.view = 'privacy';
  app.syncCms();
  await settle();
  assert.equal(app.cmsVals('privacy').cmsLegalOff, true);
  assert.equal(context.document.title, 'Privacy | E82');
});
test('home page hero blocks map to the five hero slots, tagline, intro and CTA', () => {
  const page = { slug: 'home', title: 'Home', blocks: [
    { key: 'hero-1', type: 'hero', data: { title: 'Fuel your\nmorning.', subtitle: 'Wholesome food, great coffee.', image_url: '/img/menu/a.webp' } },
    { key: 'hero-2', type: 'hero', data: { title: 'Brunch without | the fuss.', image_url: 'javascript:alert(1)' } },
    { key: 'hero-3', type: 'hero', data: { title: 'One line' } },
    { key: 'intro', type: 'hero', data: { title: 'Simple food.', subtitle: 'Our way' } },
    { key: 'intro-cta', type: 'cta', data: { label: 'See the menu', url: '/menu' } },
    { key: 'body', type: 'rich_text', data: { text: 'ignored here' } },
  ] };
  const h = cms.homeContent(page);
  assert.equal(h.tagline, 'Wholesome food, great coffee.');
  assert.deepEqual(h.slides.map(s => s.lines), [['Fuel your', 'morning.'], ['Brunch without', 'the fuss.'], ['One line', '']]);
  assert.deepEqual(h.slides.map(s => s.image), ['/img/menu/a.webp', '', '']);
  assert.equal(h.slides[0].alt, 'Fuel your morning.');
  assert.deepEqual(h.intro, { eyebrow: 'Our way', title: 'Simple food.' });
  assert.deepEqual(h.cta, { label: 'See the menu', url: '/menu' });
  assert.deepEqual(cms.homeContent({ blocks: [] }), { tagline: '', slides: [], intro: null, cta: null });
  assert.equal(cms.homeContent(null).slides.length, 0);
});
test('the home intro keeps the built-in copy until the CMS supplies it, and the hero is posted to the iframe', async () => {
  const { app, context } = mountWithCms(cmsSettings, null);
  const before = app.cmsVals('home');
  assert.equal(before.homeTitle, 'Simple food. Good coffee. Everyday balance.');
  assert.equal(before.homeCta.href, '#menu');
  assert.equal(before.homeCtaLabel, 'Explore the menu →');
  app.state.homeCms = cms.homeContent({ blocks: [
    { key: 'hero-1', type: 'hero', data: { title: 'Fresh | every day.', subtitle: 'Hello' } },
    { key: 'intro', type: 'hero', data: { title: 'Real food.', subtitle: 'The way' } },
    { key: 'intro-cta', type: 'cta', data: { label: 'Book a table', url: '/bookings' } } ] });
  const v = app.cmsVals('home');
  assert.deepEqual([v.homeEyebrow, v.homeTitle, v.homeCtaLabel, v.homeCta.href], ['The way', 'Real food.', 'Book a table', '#reserve']);
  let posted;
  context.document.querySelector = sel => sel.startsWith('iframe') ? { contentWindow: { postMessage: (m, origin) => { posted = { m, origin }; } } } : null;
  context.location = { origin: 'https://site.example' };
  app.postHero();
  assert.equal(posted.m.type, 'e82-hero');
  assert.equal(posted.m.data.tagline, 'Hello');
  assert.deepEqual(posted.m.data.slides[0].lines, ['Fresh', 'every day.']);
});
test('an unknown CMS page path shows a not-found message, not a blank screen', async () => {
  const { app } = mountWithCms(cmsSettings, null);
  app.state.view = 'page'; app.state.pageSlug = 'gone';
  app.syncCms();
  await settle();
  assert.match(text(app.cmsVals('page').cmsArticle), /Page not found/);
});
test('forgot password posts the email with the honeypot and shows the sent state or the API error', async () => {
  saved.clear();
  const calls = fakeApi({ '/api/v1/auth/password/reset': () => [200, { message: 'If the account exists, an email will be sent.' }] });
  assert.equal((await account.requestPasswordReset('guest@example.com')).ok, true);
  assert.deepEqual(calls[0].body, { email: 'guest@example.com', website: '' });
  const ok = mountWithAccount({ requestPasswordReset: async () => ({ ok: true, status: 200, data: {} }) });
  ok.state.au = { resetEmail: ' Guest@Example.com ' };
  ok.auResetSubmit()({ preventDefault() {} });
  await settle();
  assert.equal(ok.state.auReset, 'sent');
  assert.equal(ok.state.auBusy, null);
  const limited = mountWithAccount({ requestPasswordReset: async () => ({ ok: false, status: 429, data: null }), errorsOf: () => ({ message: 'Too many attempts. Please wait a minute and try again.' }) });
  limited.state.au = { resetEmail: 'guest@example.com' };
  limited.auResetSubmit()({ preventDefault() {} });
  await settle();
  assert.notEqual(limited.state.auReset, 'sent');
  assert.match(limited.state.auSrv['r-resetEmail'], /Too many attempts/);
  const invalid = mountWithAccount({ requestPasswordReset: async () => { throw new Error('must not be called'); } });
  invalid.state.au = { resetEmail: 'not-an-email' };
  invalid.auResetSubmit()({ preventDefault() {} });
  await settle();
  assert.equal(invalid.state.auReset, undefined);
});
test('saving the profile sends name and phone to the API, never the email', async () => {
  let sent;
  const app = mountWithAccount({ updateProfile: async fields => { sent = fields; return { ok: true, status: 200, data: { ...profile, ...fields } }; } });
  app.state.user = { name: 'Test  Guest Smith', email: 'guest@example.com', phone: '0411 111 111' };
  app.saveProfile();
  await settle();
  assert.deepEqual({ ...sent }, { first_name: 'Test', last_name: 'Guest Smith', phone: '0411 111 111' });
  assert.equal(app.state.profileMsg, 'Changes saved.');
});


import * as bundled from '../website/mock-data.js';
import { publicLocations, menuText } from '../website/public-presentation.js';

for (const mode of ['api', 'cache', 'mock']) {
  test(`closed location stays hidden with ${mode} data and stale preferences`, async () => {
    const snapshot = { ...bundled };
    const adapter = readFileSync(new URL('../website/eightytwenty-data.js', import.meta.url), 'utf8')
      .replace(/^(?:import |export \{).*$/gm, '')
      .replace(/^export /gm, '');
    const context = vm.createContext({
      MOCK: snapshot, API_BASE: mode === 'mock' ? '' : 'https://api.example', publicLocations,
      localStorage: { getItem: () => mode === 'cache' ? JSON.stringify(snapshot) : null, setItem() {} },
      fetch: async () => ({ ok: mode === 'api', json: async () => snapshot }),
      setTimeout: () => 0,
    });
    const result = await new vm.Script('(async () => {' + adapter + '\nreturn {LOCATIONS, MENU, CATERING, SOURCE}; })()').runInContext(context);
    assert.equal(result.SOURCE, mode);
    assert.deepEqual(Array.from(result.LOCATIONS, l => l.id), bundled.LOCATIONS.filter(l => l.id !== 'akuna').map(l => l.id));
    assert.deepEqual(JSON.parse(JSON.stringify(result.MENU)), bundled.MENU);
    assert.deepEqual(JSON.parse(JSON.stringify(result.CATERING)), bundled.CATERING);
    storage('akuna');
    assert.equal(preferredLocation(result.LOCATIONS), 'phillip');
    assert.ok(bundled.LOCATIONS.some(l => l.id === 'akuna')); // historical snapshot isn't mutated
  });
}

for (const showPrices of [false, true]) {
  for (const width of [1440, 390]) {
    test(`menu price policy ${showPrices} at viewport ${width} preserves content and Catering`, () => {
      const { app, context } = mountWithCms({}, null);
      context.window.innerWidth = width;
      context.setInterval = () => 0;
      app.D = { ...bundled, LOCATIONS: publicLocations(bundled.LOCATIONS), SHOW_MENU_PRICES: showPrices, menuText,
        ORDER_RULES: { cardSurcharge: .0295, deliveryRate: .1 } };
      app.state.narrow = width < 900;
      app.state.view = 'menu';
      app.state.item = bundled.MENU.find(item => item.mods?.some(g => g.options.some(o => o[1])));
      const values = app.renderVals();
      assert.equal(values.showMenuPrices, showPrices);
      assert.equal(Boolean(values.itemBaseLabel), showPrices);
      assert.equal(values.itemTitleColumns, showPrices ? 'minmax(0,1fr) auto' : 'minmax(0,1fr)');
      assert.equal(values.sortOpts.some(option => option.name.startsWith('Price:')), showPrices);
      for (const item of values.sections.flatMap(section => section.items)) {
        const original = bundled.MENU.find(row => row.id === item.id);
        assert.equal(Boolean(item.priceLabel), showPrices);
        for (const field of ['img', 'price']) assert.equal(item[field], original[field]);
        assert.equal(item.name, menuText(original.name, showPrices));
        assert.equal(item.desc, menuText(original.desc, showPrices));
        assert.deepEqual(item.diet, original.diet);
      }
      for (const option of values.itemGroups.flatMap(group => group.opts)) {
        if (!showPrices) assert.equal(option.priceLabel, '');
      }
      assert.equal(values.pkg.price, bundled.CATERING[0].price);
      assert.equal(values.locations.length, bundled.LOCATIONS.length - 1);
      assert.equal(values.bkLocs.length, bundled.LOCATIONS.length - 1);
      assert.equal(values.showCartBar, false);
    });
  }
}


test('menu display copy hides embedded prices while preserving ingredients, options and quantities', () => {
  const cases = [
    ['Free Range Eggs (Poached/Fried/Scramble +$1)', 'Free Range Eggs (Poached/Fried/Scramble)'],
    ['Pulled Lamb +2 / Smoked Salmon +2 / Pulled Beef +2', 'Pulled Lamb / Smoked Salmon / Pulled Beef'],
    ['250ml/750ml · Small $5 / Large $10', '250ml/750ml · Small / Large'],
    ['Add +$3 any Fruit/Pea Protein', 'Add any Fruit/Pea Protein'],
    ['Chicken+$1 / GFO + $1.50', 'Chicken / GFO'],
    ['Soft Tacos (3pcs), 330ml · Fig + Raisin · Green + / 100% GF', 'Soft Tacos (3pcs), 330ml · Fig + Raisin · Green + / 100% GF'],
    ['Small A$5.50 / Large AUD $10.00', 'Small / Large'],
  ];
  for (const [raw, display] of cases) {
    assert.equal(menuText(raw, false), display);
    assert.equal(menuText(raw, true), raw);
  }
});

test('every menu card, featured item and detail uses price-free copy without mutating source data', () => {
  const { app } = mountWithCms({}, null);
  const before = JSON.stringify(bundled.MENU);
  app.D = { ...bundled, LOCATIONS: publicLocations(bundled.LOCATIONS), SHOW_MENU_PRICES: false, menuText,
    ORDER_RULES: { cardSurcharge: .0295, deliveryRate: .1 } };
  app.bkCarousel = () => null;
  let count = 0;
  for (const category of bundled.CATEGORIES) {
    app.state.cat = category.id;
    const values = app.renderVals();
    for (const card of values.sections.flatMap(section => section.items)) {
      count++;
      assert.doesNotMatch(card.name + ' ' + card.desc, /\$|\+\s*\d/);
      app.state.item = bundled.MENU.find(row => row.id === card.id);
      const detail = app.renderVals();
      assert.equal(detail.item.name, card.name);
      assert.equal(detail.item.desc, card.desc);
      assert.equal(detail.itemBaseLabel, '');
    }
    for (const card of values.featured) assert.doesNotMatch(card.name + ' ' + card.desc, /\$|\+\s*\d/);
  }
  assert.equal(count, 84);
  assert.equal(JSON.stringify(bundled.MENU), before);
});

test('café status and the wipe-reveal rail follow the app rules', () => {
  const { app, context } = mountWithCms({}, null);
  context.setTimeout = () => 0;
  context.clearTimeout = () => {};
  context.setInterval = () => 0;
  context.clearInterval = () => {};
  app.D = { ...bundled, LOCATIONS: publicLocations(bundled.LOCATIONS), SHOW_MENU_PRICES: false, menuText,
    ORDER_RULES: { cardSurcharge: .0295, deliveryRate: .1 } };
  const daily = { id: 'x', hours: 'Mon–Sun 6:00am – 3:00pm' };
  const weekdays = { id: 'civic', hours: 'Mon–Fri 7:00am – 3:00pm · Sat–Sun closed' };
  const tuesday10 = new Date(2026, 10, 3, 10, 0); // Tue 3 Nov 2026
  const tuesday16 = new Date(2026, 10, 3, 16, 0);
  const saturday = new Date(2026, 10, 7, 10, 0);
  assert.equal(JSON.stringify(app.cafeStatus(daily, tuesday10)), JSON.stringify({ tone: 'open', label: 'Open until 3pm', color: '#3E6B47' }));
  assert.equal(app.cafeStatus(daily, tuesday16).label, 'Closed · opens 6am');
  assert.equal(app.cafeStatus(weekdays, saturday).label, 'Closed · opens 7am');
  assert.equal(app.cafeStatus({ id: 'akuna', tbc: true, hours: 'Hours to be confirmed' }).label, 'Opening soon');
  // The rail only cycles public, open cafés and wraps at both ends.
  const n = app.railItems().length;
  assert.equal(n, bundled.LOCATIONS.filter(l => l.id !== 'akuna' && !l.tbc).length);
  app.state.view = 'locations';
  app.state.ready = true;
  app.railGo(n - 1);
  assert.equal(app.state.rail.i, n - 1);
  app.railGo(n);
  assert.equal([app.state.rail.i, app.state.rail.prev, app.state.rail.tick].join(','), [0, n - 1, 2].join(','));
  app.railGo(-1);
  assert.equal(app.state.rail.i, n - 1);
  app.railPause();
  assert.equal(app.railRunning(), false);
  app.state.rail.paused = false;
  assert.equal(app.railRunning(), true);
  app.state.view = 'home';
  assert.equal(app.railRunning(), false);
  const vals = (app.state.view = 'locations', app.renderVals());
  assert.ok(vals.cafesView && vals.cafeDetailView !== undefined);
});
