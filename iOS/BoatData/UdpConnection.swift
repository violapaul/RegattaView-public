//
//  UdpConnection.swift
//  BoatData
//
//  Created by Paul Viola on 4/6/24.
//  Updated: UDP client that sends hello packets to register with server
//

import Foundation
import Network
import SwiftUI  // For ConnectionHistory

// UDP connection - preferred for low-latency instrument data
// Phone sends periodic "hello" packets to register with BoatBrain
// BoatBrain sends data back to the hello sender's address
class UdpConnection: BoatBrainConnection {
    var connection: NWConnection?
    var address: String
    var port: Int
    var helloTimer: DispatchSourceTimer?
    static let HELLO_INTERVAL: Double = 2.0  // Send hello every 2 seconds

    init?(address: String = "192.168.4.1", port: Int, onData: @escaping (Data) -> Void, onError: @escaping () -> Void, onReady: @escaping () -> Void) {
        self.address = address
        self.port = port
        super.init(onData: onData, onError: onError, onReady: onReady)

        logInfo("UDP connecting to \(address):\(port)")
        updateState(.connecting)

        guard port > 0 && port <= 65535,
              let netPort = NWEndpoint.Port(rawValue: UInt16(port)) else {
            logError("Invalid port: \(port)")
            updateState(.failed)
            return nil
        }

        // Create UDP "connection" - this sets up a socket bound to the remote endpoint
        let params = NWParameters.udp
        params.allowLocalEndpointReuse = true
        let newConnection = NWConnection(host: NWEndpoint.Host(address), port: netPort, using: params)
        self.connection = newConnection

        newConnection.stateUpdateHandler = { [weak self] state in
            self?.stateDidChange(to: state)
        }

        newConnection.start(queue: .global())
    }

    func stateDidChange(to state: NWConnection.State) {
        switch state {
        case .ready:
            logInfo("UDP ready to \(address):\(port) - starting hello timer and receive loop")
            updateState(.connected)
            receiveCount = 0
            startHelloTimer()
            receiveData()
            onReady()
        case .failed(let error):
            logWarn("UDP failed: \(error)")
            updateState(.failed)
            stopHelloTimer()
            onError()
        case .cancelled:
            logInfo("UDP cancelled")
            updateState(.cancelled)
            stopHelloTimer()
        case .waiting(let error):
            logInfo("UDP waiting: \(error)")
            updateState(.waiting)
        case .preparing:
            logInfo("UDP preparing...")
            updateState(.connecting)
        default:
            logInfo("UDP state: \(state)")
            break
        }
    }

    func startHelloTimer() {
        stopHelloTimer()
        let timer = DispatchSource.makeTimerSource(queue: .global())
        timer.schedule(deadline: .now(), repeating: UdpConnection.HELLO_INTERVAL)
        timer.setEventHandler { [weak self] in
            self?.sendHello()
        }
        timer.resume()
        helloTimer = timer
        logInfo("UDP hello timer started (interval: \(UdpConnection.HELLO_INTERVAL)s)")
    }

    func stopHelloTimer() {
        helloTimer?.cancel()
        helloTimer = nil
    }

    var helloCount = 0

    func sendHello() {
        guard let connection = connection else {
            logWarn("UDP sendHello: connection is nil")
            return
        }
        // Send a simple hello packet to register with the server
        let hello = "HELLO".data(using: .utf8)!
        helloCount += 1
        let count = helloCount
        connection.send(content: hello, completion: .contentProcessed { [weak self] error in
            if let error = error {
                logWarn("UDP hello #\(count) send failed: \(error)")
                ConnectionHistory.shared.log(.error, "Hello #\(count) failed: \(error.localizedDescription)")
                // Connection may be dead, trigger reconnect
                self?.onError()
            } else if count <= 3 {
                logInfo("UDP hello #\(count) sent OK")
                ConnectionHistory.shared.log(.dataReceived, "Hello #\(count) sent")
            }
        })
    }

    var receiveCount = 0

    func receiveData() {
        guard let connection = connection else {
            logWarn("UDP receiveData: connection is nil")
            return
        }
        connection.receiveMessage { [weak self] data, context, isComplete, error in
            guard let self = self else { return }

            self.receiveCount += 1

            if let data = data, !data.isEmpty {
                if self.receiveCount <= 3 {
                    logInfo("UDP received \(data.count) bytes (packet #\(self.receiveCount))")
                    ConnectionHistory.shared.log(.dataReceived, "Pkt #\(self.receiveCount): \(data.count) bytes")
                }
                self.onData(data)
            } else if data == nil || data!.isEmpty {
                logWarn("UDP receiveMessage: empty data, isComplete=\(isComplete)")
                ConnectionHistory.shared.log(.error, "Empty UDP packet")
            }

            if let error = error {
                logWarn("UDP receive error: \(error)")
                // For UDP, don't stop receiving on transient errors
                // Only stop if connection is truly dead
                if case .posix(let code) = error as? NWError, code == .ECANCELED {
                    logWarn("UDP connection cancelled, stopping receive")
                    return
                }
            }

            // Always continue receiving unless connection is cancelled
            if self.connection != nil {
                self.receiveData()
            } else {
                logWarn("UDP connection became nil, stopping receive loop")
            }
        }
    }

    override func close() {
        logInfo("UDP connection closing")
        stopHelloTimer()
        connection?.cancel()
        connection = nil
        updateState(.disconnected)
    }
}
