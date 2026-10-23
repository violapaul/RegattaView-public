//
//  NetworkConnection.swift
//  BoatStart
//
//  Created by Paul Viola on 10/24/23.
//

import Foundation
import Network

// Connection state for UI display
enum ConnectionState: String {
    case disconnected = "Disconnected"
    case connecting = "Connecting..."
    case connected = "Connected"
    case waiting = "Waiting for network"
    case failed = "Failed"
    case cancelled = "Cancelled"
}

// Create a superclass for
class BoatBrainConnection {
    var onData : (Data) -> Void
    var onError : () -> Void
    var onReady : () -> Void
    var onStateChange: ((ConnectionState) -> Void)?
    var connectionState: ConnectionState = .disconnected

    init(onData: @escaping (Data) -> Void, onError: @escaping () -> Void, onReady: @escaping () -> Void) {
        self.onData = onData
        self.onError = onError
        self.onReady = onReady
    }

    // force definition of close
    func close() {
        logError("close() not implemented")
    }

    func updateState(_ state: ConnectionState) {
        connectionState = state
        onStateChange?(state)
    }
}

// TCP connection - legacy, has head-of-line blocking issues
// See UdpConnection.swift for the preferred UDP implementation
class NetworkConnection : BoatBrainConnection {
    var connection : NWConnection?
    // function to callback after the connection is closed
    var done = false
    var address : String
    var port : Int
    var maxLength : Int
    var connectionTimeout: DispatchWorkItem?
    static let CONNECT_TIMEOUT_SECONDS = 10.0


    init?(address: String, port: Int, maxLength: Int, onData: @escaping (Data) -> Void, onError: @escaping () -> Void, onReady: @escaping () -> Void) {
        self.address = address
        self.port = port
        self.maxLength = maxLength
        super.init(onData: onData, onError: onError, onReady: onReady)

        logInfo("Connecting to \(address):\(port)")
        updateState(.connecting)

        // Validate port
        guard port > 0 && port <= 65535,
              let netPort = NWEndpoint.Port(rawValue: UInt16(port)) else {
            logError("Invalid port: \(port)")
            updateState(.failed)
            return nil
        }

        // Create a TCP connection to the server endpoint.
        let newConnection = NWConnection(host: NWEndpoint.Host(address), port: netPort, using: .tcp)
        self.connection = newConnection

        // Set the state update handler
        newConnection.stateUpdateHandler = stateDidChange

        // Start connection timeout
        startConnectionTimeout()

        // Start the connection
        newConnection.start(queue: .global())
    }

    func startConnectionTimeout() {
        connectionTimeout?.cancel()
        let workItem = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            if self.connectionState == .connecting || self.connectionState == .waiting {
                logWarn("Connection timeout after \(NetworkConnection.CONNECT_TIMEOUT_SECONDS)s")
                self.updateState(.failed)
                self.connection?.cancel()
                self.onError()
            }
        }
        connectionTimeout = workItem
        DispatchQueue.global().asyncAfter(
            deadline: .now() + NetworkConnection.CONNECT_TIMEOUT_SECONDS,
            execute: workItem
        )
    }

    func cancelConnectionTimeout() {
        connectionTimeout?.cancel()
        connectionTimeout = nil
    }

    // Define a function to handle incoming data
    func receiveData() {
        guard let connection = self.connection else {
            logError("Connection is nil, cannot receive data")
            return
        }
        connection.receive(minimumIncompleteLength: 0, maximumLength: self.maxLength) { [weak self] (data, context, isComplete, error) in
            guard let self = self else { return }
            // Check if there is any data
            if let data = data, !data.isEmpty {
                self.onData(data)
            }
            // Check if the connection is complete
            if isComplete {
                // Close the connection
                self.connection?.cancel()
                logWarn("Connection closed, trying to reopen!")
                self.onError()
                self.done = true
            } else if error == nil {
                self.receiveData()
            }
        }
    }
    
    // Define a function to handle connection state changes
    func stateDidChange(to state: NWConnection.State) {
        switch state {
        case .setup:
            logInfo("Connection is setting up")
            updateState(.connecting)
        case .waiting(let error):
            logInfo("Connection is waiting for network: \(error)")
            updateState(.waiting)
            // Don't call onError immediately for waiting - let timeout handle it
        case .preparing:
            logInfo("Connection is preparing")
            updateState(.connecting)
        case .ready:
            logInfo("Connection is ready")
            cancelConnectionTimeout()
            updateState(.connected)
            self.onReady()
            // Start receiving data
            self.receiveData()
        case .failed(let error):
            logWarn("Connection failed with error: \(error)")
            cancelConnectionTimeout()
            updateState(.failed)
            self.done = true
            self.onError()
        case .cancelled:
            logWarn("Connection is cancelled, just accepting it.")
            cancelConnectionTimeout()
            updateState(.cancelled)
            self.done = true
            // self.onError()
        @unknown default:
            logWarn("Unknown connection state")
            updateState(.failed)
            self.onError()
        }
    }

    override func close() {
        cancelConnectionTimeout()
        self.connection?.cancel()
    }
}
