package com.quant.api.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.math.BigDecimal;
import java.time.LocalDate;

@Entity
@Table(name = "portfolio_snapshots")
@Getter
@NoArgsConstructor
public class PortfolioSnapshot {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false)
    private LocalDate snapshotDate;

    @Column(nullable = false, length = 10)
    private String mode;

    @Column(nullable = false, precision = 18, scale = 4)
    private BigDecimal totalValue;

    @Column(nullable = false, precision = 18, scale = 4)
    private BigDecimal cashAmount;

    @Column(nullable = false, precision = 18, scale = 4)
    private BigDecimal positionValue;

    @Column(precision = 18, scale = 4)
    private BigDecimal realizedPnl;

    @Column(precision = 18, scale = 4)
    private BigDecimal cumulativePnl;
}
