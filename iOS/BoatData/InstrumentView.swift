//
//  InstrumentView.swift
//  BoatData
//
//  Created by Paul Viola on 10/24/23.
//

import SwiftUI

struct Instrument: View {
    var name: String
    var sub = ""
    var value: String

    var body: some View {
        // align bottom of text
        HStack (alignment: .bottom, spacing: 0) {
            Text(name)
                .font(Font.system(size: 35).monospacedDigit())
            Text(sub)
                .font(Font.system(size: 15).monospacedDigit())
            Spacer()
            Text(value)
        }
        .padding(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.gray, lineWidth: 2)
        )
    }
}

struct InstrumentView: View {
    
    @ObservedObject var boatData: DataManager

    var body: some View {
        let blockSpace = 10.0;
        VStack {
            Spacer()
            HStack {
                Spacer(minLength: blockSpace)
                VStack (spacing: 10.0) {
                    Instrument(name: "HDG", sub: "m", value: Ux.pf(boatData.currentInstruments.mhdg, 0))
                    Instrument(name: "SOG", value: Ux.pf(boatData.currentInstruments.sog, 1))
                    Instrument(name: "COG", value: Ux.pf(boatData.currentInstruments.cog, 0))
                }
                Spacer(minLength: blockSpace)
                VStack (spacing: 10.0) {
                    Instrument(name: "AWA", value: Ux.pf(boatData.currentInstruments.awa, 0))
                    Instrument(name: "AWS", value: Ux.pf(boatData.currentInstruments.aws, 1))
                    Instrument(name: "SPD", value: Ux.pf(boatData.currentInstruments.spd, 1))
                    
                }
                Spacer(minLength: blockSpace)
                VStack (spacing: 10.0) {
                    Instrument(name: "TWD", sub: "m", value: Ux.pf(boatData.currentInstruments.mtwd, 0))
                    Instrument(name: "TWS", value: Ux.pf(boatData.currentInstruments.tws, 1))
                    Instrument(name: "TWA", value: Ux.pf(boatData.currentInstruments.twa, 0))
                }
                Spacer(minLength: 55.0)
            }
            Spacer()
            HStack {
                Spacer(minLength: 100)
                Instrument(name: "Depth", value: Ux.pf(boatData.currentInstruments.trueDepth, 1))
                    .frame(width: 300)
                Spacer(minLength: 100)
            }

        }
        .font(Font.system(size: 50).monospacedDigit())
        .edgesIgnoringSafeArea(.all)
    }
    

}

struct InstrumentView_Previews: PreviewProvider {
    static var previews: some View {
        InstrumentView(boatData: DataManager())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}

