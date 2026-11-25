// The request map's pure parts (origins.ts). Hermetic.

import { load, report } from '../../check/harness.mjs'

const O = await load(import.meta.url, './origins.ts')
const { check, done } = report()

check(O.placeLabel({ city: 'Paris', region: 'Île-de-France', country: 'FR' }) === 'Paris, Île-de-France, FR', 'placeLabel all parts')
check(O.placeLabel({ city: 'Austin', region: null, country: 'US' }) === 'Austin, US', 'placeLabel missing region')
check(O.placeLabel({ city: null, region: '', country: null }) === 'unnamed place', 'placeLabel nothing')
check(O.placeLabel({ label: 'Lab', city: 'X', region: null, country: 'CA' }) === 'Lab', 'placeLabel prefers a known place\'s own label')
check(O.placeLabel({ label: ' ', city: 'X', region: null, country: 'CA' }) === 'X, CA', 'placeLabel ignores a blank label')

check(O.precisionLabel('database') === 'city-level', 'precisionLabel database is city-level')
check(O.precisionLabel('place') === 'known place', 'precisionLabel place is known place')

check(O.detailZoom('place') === 13, 'detailZoom: a known place opens at street level')
check(O.detailZoom('database') === 9, 'detailZoom: a city-level point opens on the city, never a street')
check(O.coordText(45.50171, -73.56734, 'place') === '45.5017, -73.5673', 'coordText: a known place to 4 decimals')
check(O.coordText(45.50171, -73.56734, 'database') === '45.50, -73.57', 'coordText: a city-level point to 2 decimals')
check(O.pickText(45.501712345) === '45.50171' && O.pickText(-73.5) === '-73.5', 'pickText rounds a click to 5 decimals')
check(O.precisionLabel('coordinator') === 'city-level' && O.precisionLabel('relay') === 'region-level', 'precisionLabel: this network is city-level, a relay a region')
check(O.detailZoom('coordinator') === 9 && O.detailZoom('relay') === 6, 'detailZoom: a relay region opens on the region')
check(O.coordText(25.761, -80.191, 'relay') === '25.8, -80.2', 'coordText: a relay region to 1 decimal')
check(O.zoomTowardPin(2) === 6 && O.zoomTowardPin(10) === 14 && O.zoomTowardPin(14) === 15 && O.zoomTowardPin(15) === 15, 'zoomTowardPin: four levels closer, capped at the tile limit')

const at = (lat, lon, label, source = 'place') => ({ lat, lon, city: null, region: null, country: null, label, source })
const hop = (role, name, location, attempt = null) => ({ role, name, address: null, host: null, location, attempt, status: null, ms: null, target_id: null, note: null })
check(O.hopTitle(hop('client', '1.2.3.4', null)) === 'Client' && O.hopTitle(hop('gateway', 'spark-01', null)) === 'Gateway spark-01', 'hopTitle: client and gateway')
check(O.hopTitle(hop('node', 'ws-3090', null, 2)) === 'Attempt 2 · ws-3090', 'hopTitle: an attempt names its server')
const home = at(45.5, -73.6, 'Home')
const rv = O.routeView([
  hop('client', '100.67.68.35', home),
  hop('gateway', 'spark-01', home),
  hop('node', 'spark-01', home, 1),
  hop('provider', 'openrouter', at(37.77, -122.42, null, 'database'), 2),
  hop('provider', 'gone', null, 3),
])
check(rv.points.length === 2, `routeView merges hops at one place: ${rv.points.length} points`)
check(rv.points[0].tooltip === 'Client, Gateway spark-01, Attempt 1 · spark-01 — Home', `routeView names every hop at a place: ${rv.points[0].tooltip}`)
check(JSON.stringify(rv.path) === '[[45.5,-73.6],[37.77,-122.42]]', `routeView draws the line in order, without repeating a place: ${JSON.stringify(rv.path)}`)
check(O.routeView([hop('client', 'x', null)]).points.length === 0, 'routeView leaves out what nothing places')
const back = O.routeView([hop('client', 'a', home), hop('provider', 'p', at(1, 2, 'P'), 1), hop('node', 'n', home, 2)])
check(back.points.length === 2 && back.path.length === 3, 'routeView: a return to an earlier place is drawn back to it')

const cidrs = (ip) => O.pinRanges(ip).map((r) => r.cidr).join(' ')
check(cidrs('203.0.113.5') === '203.0.113.5/32 203.0.113.0/24', `pinRanges IPv4: ${cidrs('203.0.113.5')}`)
check(cidrs('::ffff:10.1.2.3') === '10.1.2.3/32 10.1.2.0/24', `pinRanges IPv4-mapped is IPv4: ${cidrs('::ffff:10.1.2.3')}`)
check(cidrs('2001:DB8:0:0:1:0:0:0001') === '2001:db8::1:0:0:1/128 2001:db8::/64', `pinRanges IPv6, first longest zero run: ${cidrs('2001:DB8:0:0:1:0:0:0001')}`)
check(cidrs('::1') === '::1/128 ::/64', `pinRanges loopback: ${cidrs('::1')}`)
check(cidrs('1:0:0:0:0:0:0:0') === '1::/128 1::/64', `pinRanges trailing zeros: ${cidrs('1:0:0:0:0:0:0:0')}`)
check(cidrs('1:2:3:4:5:6:7:8') === '1:2:3:4:5:6:7:8/128 1:2:3:4::/64', `pinRanges no zeros: ${cidrs('1:2:3:4:5:6:7:8')}`)
check(cidrs('1:0:2:3:4:5:6:7') === '1:0:2:3:4:5:6:7/128 1:0:2:3::/64', `pinRanges a lone zero stays: ${cidrs('1:0:2:3:4:5:6:7')}`)
check(['', 'garbage', '256.1.1.1', 'fe80::1%eth0', '1::2::3', '1:2:3:4:5:6:7:8:9'].every((ip) => O.pinRanges(ip).length === 0), 'pinRanges refuses what does not parse')
check(O.pinRanges('8.8.8.8')[1].label === 'Its /24 (8.8.8.0/24)', 'pinRanges labels the block')

const a = { cidr: '10.0.0.0/8', label: 'A', lat: 1, lon: 2 }
const b = { cidr: '10.1.0.0/16', label: 'B', lat: 3, lon: 4 }
const moved = O.withPlace([a, b], { ...a, lat: 9 })
check(JSON.stringify(moved.map((p) => [p.cidr, p.lat])) === '[["10.0.0.0/8",9],["10.1.0.0/16",3]]', 'withPlace replaces the same range in place')
check(O.withPlace([a], b).length === 2 && O.withPlace([a], b)[1] === b, 'withPlace appends a new range')

check(O.markerRadius(0, 10) === 4, 'markerRadius(0, 10) is 4')
check(O.markerRadius(10, 10) === 18, 'markerRadius(10, 10) is 18')
check(O.markerRadius(3, 0) === 4, 'markerRadius with max 0 is 4')

check(O.fitBounds([]) === null, 'fitBounds none is null')
check(JSON.stringify(O.fitBounds([{ lat: 10, lon: 20 }])) === '[[8,18],[12,22]]', 'fitBounds one pads 2 degrees')
const many = O.fitBounds([{ lat: 0, lon: 0 }, { lat: 80, lon: 100 }])
check(JSON.stringify(many) === '[[-8,-10],[85,110]]', `fitBounds many pads 10% and clamps: ${JSON.stringify(many)}`)

const zero = { located: 0, local: 0, unlocated: 0, unknown: 0 }
check(O.originsSummary(zero, 0) === 'No requests in this window.', 'originsSummary all zero')
const mixed = O.originsSummary({ located: 5, local: 2, unlocated: 1, unknown: 3 }, 2)
check(mixed === '5 requests from 2 places · 2 from the local network · 1 with no location in the database · 3 with no client address.', `originsSummary mixed: ${mixed}`)
check(O.originsSummary({ ...zero, located: 1 }, 1) === '1 request from 1 place.', 'originsSummary singular')
check(O.originsSummary({ ...zero, local: 4 }, 0) === '4 from the local network.', 'originsSummary local only')

const db = { state: 'ready', source: 'dbip-city-lite', build: '2026-11-01', path: '/d/g.mmdb', detail: null, downloading: false, last_error: null, retry_in_s: 0 }
check(O.databaseSentence(db) === 'Locations from DB-IP City Lite, build 2026-11-01.', 'databaseSentence ready')
check(O.databaseSentence({ ...db, state: 'downloading', downloading: true }) === 'Downloading the IP location database…', 'databaseSentence downloading')
check(O.databaseSentence({ ...db, state: 'failed', detail: 'bad file' }) === 'The IP location database could not be read: bad file', 'databaseSentence failed')
check(O.databaseSentence({ ...db, state: 'missing', last_error: 'x answered 403', retry_in_s: 1800 }) === 'The IP location database could not be downloaded: x answered 403 Retrying in 30 min.', 'databaseSentence download error')
check(O.databaseSentence({ ...db, state: 'missing', pinned: true }) === 'No IP location database at /d/g.mmdb (DERATE_GEOIP_DB).', 'databaseSentence pinned')
check(O.databaseSentence({ ...db, state: 'missing' }) === 'No IP location database yet.', 'databaseSentence missing')

check(O.tileUrlTemplate('http://h:1') === 'http://h:1/api/map/tiles/{z}/{x}/{y}', 'tileUrlTemplate keeps braces literal')
check(O.tileUrlTemplate('') === '/api/map/tiles/{z}/{x}/{y}', 'tileUrlTemplate same origin')

done()
