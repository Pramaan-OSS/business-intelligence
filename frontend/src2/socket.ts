import { io, Socket } from 'socket.io-client'

let socket: Socket

export function getSocket() {
	if (socket) {
		return socket
	}

	const siteName = window.site_name || import.meta.env.VITE_SITE_NAME || window.location.hostname
	const url = `${window.location.origin}/${siteName}`
	socket = io(url, {
		withCredentials: true,
		reconnectionAttempts: 5,
	})
	return socket
}
