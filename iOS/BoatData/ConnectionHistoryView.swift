//
//  ConnectionHistoryView.swift
//  BoatData
//
//  Created by Paul Viola on 4/14/26.
//

import SwiftUI

/// A single event in the connection history
struct ConnectionEvent: Identifiable {
    let id = UUID()
    let timestamp: Date
    let type: EventType
    let message: String

    enum EventType {
        case connected
        case disconnected
        case error
        case reconnecting
        case dataReceived
        case watchdog
    }

    var icon: String {
        switch type {
        case .connected: return "checkmark.circle.fill"
        case .disconnected: return "xmark.circle.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .reconnecting: return "arrow.clockwise.circle.fill"
        case .dataReceived: return "antenna.radiowaves.left.and.right"
        case .watchdog: return "eye.fill"
        }
    }

    var color: Color {
        switch type {
        case .connected, .dataReceived: return .green
        case .disconnected: return .gray
        case .error: return .red
        case .reconnecting: return .yellow
        case .watchdog: return .orange
        }
    }
}

/// Manages connection history - singleton accessible from DataManager
class ConnectionHistory: ObservableObject {
    static let shared = ConnectionHistory()

    @Published var events: [ConnectionEvent] = []
    static let maxEvents = 100

    private let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    func log(_ type: ConnectionEvent.EventType, _ message: String) {
        let event = ConnectionEvent(timestamp: Date(), type: type, message: message)
        DispatchQueue.main.async {
            self.events.insert(event, at: 0)
            if self.events.count > ConnectionHistory.maxEvents {
                self.events.removeLast()
            }
        }
    }

    func formatTime(_ date: Date) -> String {
        return dateFormatter.string(from: date)
    }

    func clear() {
        DispatchQueue.main.async {
            self.events.removeAll()
        }
    }
}

struct ConnectionHistoryView: View {
    @ObservedObject var boatData: DataManager
    @ObservedObject var history = ConnectionHistory.shared
    @ObservedObject var wifi = WiFiHelper.shared

    var body: some View {
        VStack(spacing: 0) {
            // Header with current status
            HStack {
                VStack(alignment: .leading) {
                    HStack {
                        Circle()
                            .fill(statusColor)
                            .frame(width: 12, height: 12)
                        Text(boatData.connectionState.rawValue)
                            .font(.headline)
                        Text("WiFi: \(wifi.ssid)")
                            .font(.caption)
                            .foregroundColor(wifi.ssid == "BoatBrain" ? .green : .orange)
                    }
                    Text("Resets: \(boatData.currentInstruments.resetCount)  Retries: \(boatData.reconnectAttempts)")
                        .font(.caption)
                        .foregroundColor(.gray)
                }
                Spacer()
                if !wifi.locationAuthorized {
                    Button("Allow") {
                        wifi.requestPermission()
                    }
                    .font(.caption)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(Color.blue.opacity(0.3))
                    .cornerRadius(4)
                }
                Button("Clear") {
                    history.clear()
                }
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.gray.opacity(0.3))
                .cornerRadius(4)
            }
            .padding()
            .background(Color.black.opacity(0.3))
            .onAppear {
                wifi.updateSSID()
            }

            // Event list
            if history.events.isEmpty {
                Spacer()
                Text("No events yet")
                    .foregroundColor(.gray)
                Spacer()
            } else {
                List(history.events) { event in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: event.icon)
                            .foregroundColor(event.color)
                            .frame(width: 20)

                        VStack(alignment: .leading, spacing: 2) {
                            Text(event.message)
                                .font(.system(size: 14, design: .monospaced))
                                .foregroundColor(.white)
                            Text(history.formatTime(event.timestamp))
                                .font(.system(size: 11))
                                .foregroundColor(.gray)
                        }
                    }
                    .listRowBackground(Color.clear)
                }
                .listStyle(.plain)
            }
        }
    }

    var statusColor: Color {
        switch boatData.connectionState {
        case .connected: return .green
        case .connecting, .waiting: return .yellow
        case .failed: return .red
        case .disconnected, .cancelled: return .gray
        }
    }
}

struct ConnectionHistoryView_Previews: PreviewProvider {
    static var previews: some View {
        ConnectionHistoryView(boatData: DataManager())
            .preferredColorScheme(.dark)
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
