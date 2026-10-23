//
//  ContentView.swift
//  BoatData
//
//  Created by Paul Viola on 10/22/23.
//

import SwiftUI

// Configuration keys for UserDefaults
enum ConfigKeys {
    static let address = "boatbrain_address"
    static let port = "boatbrain_port"
    static let defaultAddress = "192.168.4.1"
    static let defaultPort = 101  // UDP port
}

let darkRed = Color(red: 0.6, green: 0.0, blue: 0.0)


struct ContentView: View {
    @StateObject var boatData: DataManager

    init() {
        // Load config from UserDefaults with defaults
        let savedAddress = UserDefaults.standard.string(forKey: ConfigKeys.address) ?? ConfigKeys.defaultAddress
        let savedPort = UserDefaults.standard.integer(forKey: ConfigKeys.port)
        let port = savedPort > 0 ? savedPort : ConfigKeys.defaultPort

        _boatData = StateObject(wrappedValue: DataManager(address: savedAddress, port: port, source: .boatBrainUdp))
    }

    @State private var activeTag = 3
    @State private var wifiPassword = ""
    @State private var wifiMessage = ""

    @Environment(\.scenePhase) var scenePhase

    var body: some View {
        let isShallow = boatData.currentInstruments.trueDepth < 5
        TabView (selection: $activeTag) {
            InstrumentStrip(boatData: boatData)
                .tag(1)
            InstrumentView(boatData: boatData)
                .tag(2)
            DebugView(boatData: boatData)
                .tag(3)
            ConnectionHistoryView(boatData: boatData)
                .tag(4)
            LogView()
                .tag(5)
            Form {
                Section("BoatBrain Wi-Fi") {
                    SecureField("Device password", text: $wifiPassword)
                    Button("Save password and connect") {
                        if WiFiHelper.shared.savePassword(wifiPassword) {
                            wifiPassword = ""
                            wifiMessage = "Password saved on this device."
                        } else { wifiMessage = "Use the provisioned 16–63 character password." }
                    }
                    Text(wifiMessage)
                }
            }.tag(7)
            if let updater = boatData.ipAddressUpdater {
                IPAddressView(updater: updater)
                    .tag(6)
            }
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        .background(isShallow ? .orange : (boatData.dataStale() ? darkRed : .black))
        // .edgesIgnoringSafeArea(.all)
    }
    // use the receive(on:) operator to switch to the main thread
//    .onReceive(nmea.$currentInstruments.receive(on: DispatchQueue.main)) { newData in
//        print("Received new data on main thread: \(newData)")
//    }
}

struct ContentView_Previews: PreviewProvider {
    static var previews: some View {
        ContentView()
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
