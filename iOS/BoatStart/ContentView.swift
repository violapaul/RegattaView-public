//
//  ContentView.swift
//  BoatStart
//
//  Created by Paul Viola on 10/30/22.
//

import Foundation
import AVFoundation
import SwiftUI

struct ContentView: View {
    
    @StateObject var boatState = BoatManager()
    @StateObject var timeManager = TimeManager(sequenceTime: 60)

    @State private var activeTag = 2

    @Environment(\.scenePhase) var scenePhase
    
    var body: some View {
        TabView (selection: $activeTag) {
            TimerView(timeManager: timeManager)
                .tag(1)
            VelocitekView(timeManager: timeManager, boatState: boatState)
                .tag(2)
            LineView2(boatState: boatState, timeManager: timeManager)
                .tag(3)
            ShiftView(boatState: boatState)
                .tag(4)
            DataView(boatState: boatState)
                .tag(5)
            InstrumentView(boatState: boatState, timeManager: timeManager)
                .tag(6)
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        .edgesIgnoringSafeArea(.all)
        .onChange(of: scenePhase) { newPhase in
            if newPhase == .active {
                print("Active")
            } else if newPhase == .inactive {
                print("Inactive Saving Track")
                boatState.saveTrack()
            } else if newPhase == .background {
                print("Background")
            }
        }
    }
}

struct ContentView_Previews: PreviewProvider {
    static var previews: some View {
        ContentView()
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
