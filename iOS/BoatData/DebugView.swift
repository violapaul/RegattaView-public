//
//  DebugView.swift
//  BoatData
//
//  Created by Paul Viola on 10/26/23.
//

import SwiftUI

struct DebugView: View {
    @ObservedObject var boatData: DataManager
    @ObservedObject var wifi = WiFiHelper.shared

    var body: some View {
        
        HStack (spacing: 50.0) {
            Grid {
                GridRow {
                    Text("Lat")
                        .gridColumnAlignment(.leading)
                    Text(Ux.pf(boatData.currentInstruments.lat, 5))
                        .gridColumnAlignment(.trailing)
                }
                GridRow {
                    Text("Lon")
                    Text(Ux.pf(boatData.currentInstruments.lon, 5))
                }
                GridRow {
                    Text("SOG")
                    Text(Ux.pf(boatData.currentInstruments.sog, 1))
                }
                GridRow {
                    Text("COG")
                    Text(Ux.pf(boatData.currentInstruments.cog, 1))
                }
                GridRow {
                    Text("HDG")
                    Text(Ux.pf(boatData.currentInstruments.hdg, 1))
                }
                GridRow {
                    Text("SPD")
                    Text(Ux.pf(boatData.currentInstruments.spd, 1))
                }

            }
            Grid {
                GridRow {
                    Text("AWA")
                        .gridColumnAlignment(.leading)
                    Text(Ux.pf(boatData.currentInstruments.awa, 1))
                        .gridColumnAlignment(.trailing)
                }
                GridRow {
                    Text("AWS")
                    Text(Ux.pf(boatData.currentInstruments.aws, 1))
                }
                GridRow {
                    Text("TWA")
                    Text(Ux.pf(boatData.currentInstruments.twa, 1))
                }
                GridRow {
                    Text("TWS")
                    Text(Ux.pf(boatData.currentInstruments.tws, 1))
                }
                GridRow {
                    Text("TWD")
                    Text(Ux.pf(boatData.currentInstruments.twd, 1))
                }
                GridRow {
                    Text("DPT")
                    Text(Ux.pf(boatData.currentInstruments.depth, 1))
                }

            }
            Grid{
                GridRow {
                    Text("WiFi")
                        .gridColumnAlignment(.leading)
                    Text(wifi.ssid.isEmpty ? "(none)" : wifi.ssid)
                        .gridColumnAlignment(.trailing)
                        .foregroundColor(wifi.ssid == "BoatBrain" ? .green : .yellow)
                }
                GridRow {
                    Text("Status")
                        .gridColumnAlignment(.leading)
                    Text(boatData.connectionState.rawValue)
                        .gridColumnAlignment(.trailing)
                        .foregroundColor(connectionColor)
                }
                GridRow {
                    Text("Retry#")
                    Text("\(boatData.reconnectAttempts)")
                }
                GridRow {
                    Text("Reset#")
                    Text("\(boatData.currentInstruments.resetCount)")
                }
                GridRow {
                    Text("Pkt/Msg")
                    HStack {
                        Text("\(boatData.currentInstruments.packetCount)")
                        Text("/")
                        Text("\(boatData.currentInstruments.msgCount)")
                    }
                }
                GridRow {
                    Text("Errors")
                    Text("\(boatData.currentInstruments.errorCount)")
                        .foregroundColor(boatData.currentInstruments.errorCount > 0 ? .red : .white)
                }
                GridRow {
                    Text("Stale")
                    HStack(spacing: 2) {
                        Text(Ux.pf(boatData.msgStats.max(), 2))
                            .foregroundColor(boatData.msgStats.max() > 2.0 ? .orange : .white)
                        Text("/")
                        Text(Ux.pf(boatData.msgStats.avg(), 2))
                    }
                }
            }
        }
        .font(Font.system(size: 25).monospacedDigit())
    }

    var connectionColor: Color {
        switch boatData.connectionState {
        case .connected:
            return .green
        case .connecting, .waiting:
            return .yellow
        case .disconnected, .cancelled:
            return .gray
        case .failed:
            return .red
        }
    }
}


struct DebugView_Previews: PreviewProvider {
    static var previews: some View {
        DebugView(boatData: DataManager())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
