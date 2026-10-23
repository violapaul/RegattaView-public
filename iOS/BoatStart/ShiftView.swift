//
//  ShiftView.swift
//  BoatStart
//
//  Created by Paul Viola on 1/9/23.
//

import SwiftUI

struct ShiftInfoView: View {
    
    var shiftDegrees: CGFloat
    
    var body: some View {
        
        GeometryReader { metrics in
            let size = metrics.size
            let boxWidth = size.width * CGFloat(min(abs(shiftDegrees), 1.0) / 2.0)
            ZStack {
                if shiftDegrees < 0.0 {
                    Rectangle()
                        .fill(.red)
                        .frame(width: boxWidth, height: size.height)
                        .position(x: size.width/2 - boxWidth/2, y: size.height/2)
                } else {
                    Rectangle()
                        .fill(.green)
                        .frame(width: boxWidth, height: size.height)
                        .position(x: size.width/2 + boxWidth/2, y: size.height/2)
                }
                HStack (spacing: 0.0) {
                    Rectangle()
                        .strokeBorder(.red, lineWidth: 3)
                        .contentShape(Rectangle())
                    Rectangle()
                        .strokeBorder(.green, lineWidth: 3)
                        .contentShape(Rectangle())
                }

                // .gesture(makeTap("boat edge tapped"))
            }
        }
    }
}

struct ShiftView: View {
    
    @ObservedObject var boatState: BoatState
    
    static let bigPoint: CGFloat = 180.0

    static let bigFont = Font
        .system(size: ShiftView.bigPoint)
        .monospacedDigit()
    
    static let buttonFont = Font
        .system(size: 80)
        .monospaced()
    
    static let tinyFont = Font
        .system(size: 30)
        .monospacedDigit()
    
    struct KnotsView: View {
        var speed: Double
        
        var body: some View {
            HStack(alignment: .lastTextBaseline) {
                Text(String(format: "%.1f", speed))
                    .frame(height: 0.85 * bigPoint)
                Text("kts")
                    .font(ShiftView.tinyFont)
            }
        }
    }
    
    struct AngleView: View {
        var degrees: Double

        var body: some View {
            let angleString = String(format: "%03d", Int(round(degrees)))
            let degreesMark = NSString(format:"%@", "\u{00B0}") as String
            
            HStack(alignment: .top) {
                Text(angleString)
                    .frame(height: 0.85 * bigPoint)
                Text(degreesMark)
                    .font(ShiftView.buttonFont)
            }
            .frame(height: 0.85 * bigPoint)
        }
    }

    
    var body: some View {

        GeometryReader { metrics in
            let size = metrics.size
            let rectHeight = size.height/6
            let shift = (boatState.state.hdg - boatState.state.afterTackHdg) / 10.0
            HStack (spacing: 0.0) {
                VStack (alignment: .trailing, spacing: 0.0) {
                    ShiftInfoView(shiftDegrees: shift)
                        .frame(width: size.width, height: rectHeight)
                    AngleView(degrees: boatState.state.hdg)
                    Rectangle().fill(.gray).frame(height:10)
                    KnotsView(speed: boatState.state.sog)
                }
                    .frame(height: 190)
                }
                .frame(
                    maxWidth: .infinity,
                    maxHeight: .infinity
                )
                .font(ShiftView.bigFont)
            }
        // .padding(-10)
        // .ignoresSafeArea()
    }
}


struct ShiftView_Previews: PreviewProvider {
    static var previews: some View {
        ShiftView(boatState: BoatState())
            .previewInterfaceOrientation(.landscapeLeft)
    }
}
