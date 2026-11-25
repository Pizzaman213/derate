// The only Leaflet import. RequestMap loads this with import() so Leaflet and
// its stylesheet are their own chunk, not part of the main bundle.
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

export default L
